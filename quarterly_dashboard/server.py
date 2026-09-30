"""Loopback-only web server and small per-stock quarterly cache."""

from __future__ import annotations

import html
import json
import shutil
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse

import requests
from plotly.offline import get_plotlyjs

from .core import build_period_rows, disclosure_reference_snapshots, disclosure_snapshots, view_rows
from .network import create_data_session
from .storage import DEFAULT_DATABASE, Database, instance_lock
from .chips import (CHIP_BASIS, chip_payload, chip_rows, fetch_chip_records, missing_chip_history,
                    shareholder_price_snapshots)
from .sources import (fetch_cash_flow_reports, fetch_daily_prices, fetch_financial_reports,
                      fetch_monthly_prices, fetch_stock_name, normalize_code, normalize_report_dates)
from .valuation import (fetch_dividend_events, fetch_dividend_yields, fetch_industry_snapshot, fetch_valuation_series,
                        merge_adjusted_prices, merge_dividend_yields, monthly_valuation, valuation_summary)


ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "fundamentals"
VALUATION_CACHE = ROOT / "data" / "valuation"
CHIP_CACHE = ROOT / "data" / "chips"
DATABASE_PATH = DEFAULT_DATABASE
TEMPLATE = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
REPORT_DATE_BASIS = "sina_same_period_shift_v1"
PRICE_REFERENCE_BASIS = "long_trading_gap_v1"
CASH_FLOW_BASIS = "sina_cash_flow_ytd_v1"
FINANCIAL_FIELDS_BASIS = "sina_margin_debt_roic_inputs_v2"
DIVIDEND_BASIS = "eastmoney_implemented_report_period_total_shares_v1"
NEW_REPORT_FIELDS = ("operating_cost_ytd", "net_profit_ytd", "monetary_funds",
                     "short_term_borrowings", "short_term_bonds",
                     "current_noncurrent_liabilities", "long_term_borrowings",
                     "bonds_payable", "lease_liabilities", "total_equity",
                     "profit_before_tax_ytd", "income_tax_expense_ytd",
                     "interest_expense_ytd", "non_operating_interest_income_ytd")
VALUATION_BASIS = "monthly_qfq_overlay_v1"
_DATA_LOCKS: dict[str, Lock] = {}


FINANCIAL_VALUES = ("revenue_ytd", "profit_ytd", "shares", "equity") + NEW_REPORT_FIELDS
CASH_VALUES = ("operating_cash_flow_ytd", "capex_ytd")


def _save_cache(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    if path.exists():
        backup = path.with_suffix(".json.bak")
        backup_temporary = path.with_suffix(".json.bak.tmp")
        shutil.copy2(path, backup_temporary)
        backup_temporary.replace(backup)
    temporary.replace(path)


def _keep_old(old: dict, message: str) -> dict:
    return {**old, "warnings": old.get("warnings", []) + [message]}


def _missing_old_values(old_reports: list[dict], fresh_by_period: dict[str, dict],
                        fields: tuple[str, ...], require_all_periods: bool = False) -> list[str]:
    missing = []
    for old_report in old_reports:
        period = old_report["period"]
        fresh = fresh_by_period.get(period)
        if fresh is None and (require_all_periods or any(old_report.get(field) is not None for field in fields)):
            missing.append(period)
        elif any(old_report.get(field) is not None and fresh.get(field) is None for field in fields):
            missing.append(period)
    return missing


def _missing_price_snapshots(old: dict, new_prices: dict, reports: list[dict]) -> list[str]:
    if old.get("price_basis") != "disclosure":
        return []
    current_dates = {report.get("publish_date") for report in reports}
    missing = []
    for key in ("raw", "qfq", "raw_reference", "qfq_reference"):
        existing = old.get("prices", {}).get(key, [])
        fresh_dates = {point.get("publish_date") for point in new_prices.get(key, [])}
        if key.endswith("_reference"):
            fresh_dates |= {point.get("publish_date") for point in new_prices.get(key[:-10], [])}
        if existing and not fresh_dates:
            missing.append(f"{key}:全部缺失")
        missing.extend(f"{key}:{point['publish_date']}" for point in existing
                       if point.get("publish_date") in current_dates
                       and point["publish_date"] not in fresh_dates)
    return missing


def _missing_valuation_history(old: dict, rows: list[dict], industry: dict, today: str) -> list[str]:
    fresh_by_month = {row["date"][:7]: row for row in rows}
    ten_years_ago = f"{int(today[:4]) - 10}{today[4:]}"
    missing = []
    for old_row in old.get("rows", []):
        month = old_row["date"][:7]
        fresh = fresh_by_month.get(month, {})
        for field in ("pe", "pb", "ps", "dividend_yield", "qfq_close"):
            if old_row.get(field) is not None and (field == "qfq_close" or old_row["date"] >= ten_years_ago):
                if fresh.get(field) is None:
                    missing.append(f"{month}:{field}")
    for field in ("pe", "pb", "ps"):
        if old.get("industry", {}).get(field) is not None and industry.get(field) is None:
            missing.append(f"industry:{field}")
    return missing


def load_valuation(code: str, reports: list[dict], refresh: bool = False) -> dict:
    """Cache compact monthly valuation rows separately from financial snapshots."""
    path = VALUATION_CACHE / f"{code}.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    today = date.today().isoformat()
    if old.get("updated_on") == today and old.get("basis") == VALUATION_BASIS and not refresh:
        return old
    session = create_data_session()
    warnings = []
    try:
        rows = monthly_valuation(fetch_valuation_series(code, session), reports)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        if old:
            return _keep_old(old, f"历史估值获取失败，保留原缓存：{exc}")
        rows = old.get("rows", [])
        warnings.append(f"历史估值获取失败，显示已有缓存：{exc}")
    try:
        dividend_yields = fetch_dividend_yields(code, session)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        if old:
            return _keep_old(old, f"历史股息率获取失败，保留原缓存：{exc}")
        dividend_yields = [{"date": row["dividend_yield_date"], "value": row["dividend_yield"]}
                           for row in old.get("rows", []) if row.get("dividend_yield_date")
                           and row.get("dividend_yield") is not None]
        warnings.append(f"历史股息率获取失败，显示已有缓存：{exc}")
    rows = merge_dividend_yields(rows, dividend_yields)
    try:
        adjusted_prices = fetch_monthly_prices(code, session, "qfq")
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        if old:
            return _keep_old(old, f"前复权月度股价获取失败，保留原缓存：{exc}")
        adjusted_prices = [{"date": row["qfq_close_date"], "close": row["qfq_close"]}
                           for row in old.get("rows", []) if row.get("qfq_close_date")
                           and row.get("qfq_close") is not None]
        warnings.append(f"前复权月度股价获取失败，显示已有缓存：{exc}")
    rows = merge_adjusted_prices(rows, adjusted_prices)
    time.sleep(1.0)  # Keep EastMoney peer and dividend requests at least a second apart.
    try:
        industry = fetch_industry_snapshot(code, session)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        if old:
            return _keep_old(old, f"行业估值获取失败，保留原缓存：{exc}")
        industry = old.get("industry", {})
        warnings.append(f"行业估值获取失败，显示已有缓存：{exc}")
    if old:
        missing = _missing_valuation_history(old, rows, industry, today)
        if missing:
            return _keep_old(old, f"估值历史覆盖不足（{len(missing)} 项），保留原缓存")
        by_month = {row["date"][:7]: dict(row) for row in old.get("rows", [])}
        for row in rows:
            month = row["date"][:7]
            previous = by_month.get(month, {})
            by_month[month] = {**previous, **{key: value for key, value in row.items()
                                            if value is not None and key != "date"},
                              "date": max(previous.get("date", row["date"]), row["date"])}
        rows = [by_month[month] for month in sorted(by_month)]
        industry = {**old.get("industry", {}),
                    **{key: value for key, value in industry.items() if value is not None}}
    data = {"rows": rows, "industry": industry, "updated_on": today, "basis": VALUATION_BASIS,
            "warnings": warnings}
    _save_cache(path, data)
    return data


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
        reference_key = f"{key}_reference"
        try:
            daily = fetch_daily_prices(code, session, adjust, dates[0], dates[-1]) if dates else []
            prices[key] = disclosure_snapshots(reports, daily)
            prices[reference_key] = disclosure_reference_snapshots(reports, daily)
        except (requests.RequestException, ValueError, KeyError) as exc:
            complete = False
            prices[key] = (old or {}).get("prices", {}).get(key, []) if (old or {}).get("price_basis") == "disclosure" else []
            prices[reference_key] = (old or {}).get("prices", {}).get(reference_key, [])
            warnings.append(f"{key} 披露日价格更新失败：{exc}")
    return prices, warnings, complete


def _migrate_cached(code: str, old: dict, path: Path, session: requests.Session) -> dict:
    old = _add_missing_name(old, path, session)
    if old.get("reports") and old.get("financial_fields_basis") != FINANCIAL_FIELDS_BASIS:
        try:
            fresh_reports = {report["period"]: report for report in fetch_financial_reports(code, session)}
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            old = _keep_old(old, f"新增财报字段获取失败：{exc}")
        else:
            missing = _missing_old_values(old["reports"], fresh_reports, NEW_REPORT_FIELDS,
                                          require_all_periods=True)
            if missing:
                old = _keep_old(old, f"新增财报字段历史覆盖不足（{len(missing)} 期），保留原缓存")
            else:
                old = {**old, "reports": [
                    {**report, **{field: value for field in NEW_REPORT_FIELDS
                                 if (value := fresh_reports[report["period"]].get(field)) is not None}}
                    for report in old["reports"]],
                       "financial_fields_basis": FINANCIAL_FIELDS_BASIS}
                _save_cache(path, old)
    cash_warnings = []
    if old.get("cash_flow_basis") != CASH_FLOW_BASIS:
        try:
            cash_flows = fetch_cash_flow_reports(code, session)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            cash_warnings.append(f"现金流量表获取失败：{exc}")
            old = {**old, "warnings": old.get("warnings", []) + cash_warnings}
        else:
            missing = _missing_old_values(old["reports"], cash_flows, CASH_VALUES)
            if missing:
                cash_warnings.append(f"现金流量表历史覆盖不足（{len(missing)} 期），保留原缓存")
                old = {**old, "warnings": old.get("warnings", []) + cash_warnings}
            else:
                old = {**old, "reports": [
                    {**report, **{field: value for field in CASH_VALUES
                                 if (value := cash_flows.get(report["period"], {}).get(field)) is not None}}
                    for report in old["reports"]],
                       "cash_flow_basis": CASH_FLOW_BASIS}
                _save_cache(path, old)
    if (old.get("report_date_basis") == REPORT_DATE_BASIS and old.get("price_basis") == "disclosure"
            and old.get("price_reference_basis") == PRICE_REFERENCE_BASIS):
        return old
    reports = (old["reports"] if old.get("report_date_basis") == REPORT_DATE_BASIS
               else normalize_report_dates(old["reports"]))
    prices, warnings, complete = _disclosure_prices(code, reports, session, old)
    missing_prices = _missing_price_snapshots(old, prices, reports) if complete else []
    if missing_prices:
        return _keep_old(old, f"价格快照历史覆盖不足（{len(missing_prices)} 项），保留原缓存")
    if not complete:
        return _keep_old(old, "披露日价格获取失败，保留原缓存：" + "；".join(warnings))
    migrated = {**old, "reports": reports, "prices": prices, "price_basis": "disclosure",
                "report_date_basis": REPORT_DATE_BASIS,
                "price_reference_basis": PRICE_REFERENCE_BASIS if complete else None,
                "warnings": cash_warnings + warnings}
    if complete:
        _save_cache(path, migrated)
    return migrated


def load_stock(code: str, refresh: bool = False) -> dict:
    code = normalize_code(code)
    path = CACHE / f"{code}.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    session = create_data_session()
    if old and not refresh:
        return _migrate_cached(code, old, path, session)
    try:
        reports = fetch_financial_reports(code, session)
    except (requests.RequestException, ValueError, KeyError) as exc:
        if old:
            return _keep_old(old, f"财报更新失败，保留原缓存：{exc}")
        raise ValueError(f"无法获取 {code} 的季度财报：{exc}") from exc
    if old:
        missing = _missing_old_values(old.get("reports", []),
                                      {report["period"]: report for report in reports}, FINANCIAL_VALUES,
                                      require_all_periods=True)
        if missing:
            return _keep_old(old, f"财报更新未覆盖已有报告期或指标（{len(missing)} 期），保留原缓存")
        old_by_period = {report["period"]: report for report in old.get("reports", [])}
        # Keep optional methodology inputs across source refreshes, including
        # explicit zero/None. Future source-provided hook values take precedence.
        for report in reports:
            previous = old_by_period.get(report["period"], {})
            for field in ("excess_cash", "non_operating_adjustments_ytd"):
                if field in previous and field not in report:
                    report[field] = previous[field]
    cash_flow_basis = None
    cash_warnings = []
    try:
        cash_flows = fetch_cash_flow_reports(code, session)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        if old:
            return _keep_old(old, f"现金流量表获取失败，保留原缓存：{exc}")
        cash_warnings.append(f"现金流量表获取失败：{exc}")
    else:
        if old:
            missing = _missing_old_values(old.get("reports", []), cash_flows, CASH_VALUES)
            if missing:
                return _keep_old(old, f"现金流量表历史覆盖不足（{len(missing)} 期），保留原缓存")
        reports = [{**report, **cash_flows.get(report["period"], {})} for report in reports]
        cash_flow_basis = CASH_FLOW_BASIS
    prices, warnings, complete = _disclosure_prices(code, reports, session, old)
    if old and not complete:
        return _keep_old(old, "披露日价格更新失败，保留原缓存：" + "；".join(warnings))
    if old:
        missing_prices = _missing_price_snapshots(old, prices, reports)
        if missing_prices:
            return _keep_old(old, f"价格快照历史覆盖不足（{len(missing_prices)} 项），保留原缓存")
    warnings = cash_warnings + warnings
    try:
        name = fetch_stock_name(code, session)
    except (requests.RequestException, ValueError, KeyError) as exc:
        name = old.get("name") if old else None
        if not name:
            warnings.append(f"股票名称获取失败：{exc}")
    data = {"code": code, "updated_at": datetime.now(timezone.utc).isoformat(),
            "name": name, "reports": reports, "prices": prices,
            "price_basis": "disclosure", "report_date_basis": REPORT_DATE_BASIS,
            "cash_flow_basis": cash_flow_basis,
            "financial_fields_basis": FINANCIAL_FIELDS_BASIS,
            "price_reference_basis": PRICE_REFERENCE_BASIS if complete else None,
            "warnings": warnings}
    if old and "dividend_events" in old:
        data["dividend_events"] = old["dividend_events"]
        data["dividend_basis"] = old.get("dividend_basis")
    _save_cache(path, data)
    return data


def load_dividends(data: dict, refresh: bool = False) -> tuple[list[dict] | None, list[str]]:
    """Cache implemented dividend events with stock fundamentals; protect older history."""
    old_events = data.get("dividend_events")
    if data.get("dividend_basis") == DIVIDEND_BASIS and old_events and not refresh:
        return old_events, []
    session = create_data_session()
    try:
        events = fetch_dividend_events(data["code"], session)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        return old_events, [f"现金分红获取失败，保留原缓存：{exc}"]
    if not events:
        if old_events:
            return old_events, ["现金分红接口返回空记录，保留原缓存"]
        return None, ["现金分红接口返回空记录，未标记为补齐完成；下次打开将自动重试"]
    if old_events:
        fresh_by_key = {(event.get("date"), event.get("report_period")): event for event in events}
        missing = [event for event in old_events
                   if (new := fresh_by_key.get((event.get("date"), event.get("report_period")))) is None
                   or (event.get("total_shares") is not None and new.get("total_shares") is None)]
        if missing:
            return old_events, [f"现金分红历史覆盖不足（{len(missing)} 笔），保留原缓存"]
    # Fetch without holding the financial lock; merge only once network work ends.
    path = CACHE / f'{data["code"]}.json'
    with _DATA_LOCKS.setdefault("financial:" + data["code"], Lock()):
        latest = _read_cache(path) or data
        data.update(latest, dividend_events=events, dividend_basis=DIVIDEND_BASIS)
        _save_cache(path, data)
    return events, []


def cached_stocks() -> list[dict]:
    stocks = []
    for path in sorted(CACHE.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
        stocks.append({"code": data["code"], "name": data.get("name")})
    return stocks


def _read_cache(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _needs_financial(data: dict) -> bool:
    return (not data or "name" not in data
            or data.get("financial_fields_basis") != FINANCIAL_FIELDS_BASIS
            or data.get("cash_flow_basis") != CASH_FLOW_BASIS
            or data.get("report_date_basis") != REPORT_DATE_BASIS
            or data.get("price_basis") != "disclosure"
            or data.get("price_reference_basis") != PRICE_REFERENCE_BASIS)


def _needs_dividends(data: dict) -> bool:
    return data.get("dividend_basis") != DIVIDEND_BASIS or not data.get("dividend_events")


def _financial_payload(data: dict) -> dict:
    if not data:
        return {"views": {}, "warnings": []}
    reports = data.get("reports", [])
    if data.get("report_date_basis") != REPORT_DATE_BASIS:
        reports = normalize_report_dates(reports)
    prices = (data.get("prices", {}) if data.get("price_basis") == "disclosure"
              and data.get("report_date_basis") == REPORT_DATE_BASIS else {})
    rows = build_period_rows(reports, prices.get("raw", []), prices.get("qfq", []),
                             prices.get("raw_reference"), prices.get("qfq_reference"),
                             data.get("dividend_events") or None)
    return {"code": data["code"], "name": data.get("name"), "updated_at": data.get("updated_at"),
            "views": {name: view_rows(rows, name) for name in ("quarter", "year", "ttm")},
            "warnings": data.get("warnings", []), "needs_dividends": _needs_dividends(data)}


def _valuation_payload(data: dict) -> dict:
    if not data:
        return {"views": {}, "warnings": []}
    industry = data.get("industry", {})
    return {"views": {
        str(years): {metric: valuation_summary(data.get("rows", []), metric, years, industry)
                     for metric in ("pe", "pb", "ps", "dividend_yield")}
        for years in (3, 5, 10)},
        "updated_on": data.get("updated_on"), "peer_count": industry.get("peer_count"),
        "warnings": data.get("warnings", [])}


def load_chips(code: str, section: str, refresh: bool = False) -> dict:
    """Cache full raw histories independently; only the response is windowed."""
    path = CHIP_CACHE / section / f"{code}.json"
    old = _read_cache(path)
    today = date.today().isoformat()
    if old.get("updated_on") == today and old.get("basis") == CHIP_BASIS and not refresh:
        return chip_payload(old, section)
    try:
        with create_data_session() as session:
            records = fetch_chip_records(code, section, session)
            prices = []
            if section == "shareholders" and records:
                dates = sorted(row["END_DATE"][:10] for row in records)
                earliest = (date.fromisoformat(dates[0]) - timedelta(days=15)).isoformat()
                daily = fetch_daily_prices(code, session, "", earliest, dates[-1])
                if not daily:
                    raise ValueError("腾讯未返回股东人数对应的未复权价格")
                prices = shareholder_price_snapshots(records, daily)
        fresh = chip_rows(records, section, prices)
        old_rows = chip_rows(old.get("records", []), section, old.get("prices"))
        if section == "shareholders" and old.get("basis") != CHIP_BASIS:
            # Prototype caches used the detail table's price without a trading
            # date. It is not comparable to the current 15-day snapshot rule.
            old_rows = [{**row, "close": None} for row in old_rows]
        missing = missing_chip_history(old_rows, fresh, section)
        if missing:
            raise ValueError(f"历史覆盖不足（{len(missing)} 个日期）")
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        if old:
            return chip_payload(_keep_old(old, f"筹码更新失败，保留原缓存：{exc}"), section)
        raise ValueError(f"无法获取 {code} 的筹码数据：{exc}") from exc
    data = {"code": code, "basis": CHIP_BASIS, "records": records,
            "updated_on": today, "empty": not records, "warnings": []}
    if section == "shareholders":
        data["prices"] = prices
    _save_cache(path, data)
    return chip_payload(data, section)


def load_chart_data(code: str, section: str, refresh: bool = False) -> dict:
    """Network work runs in separate requests; serialize writes to each stock cache."""
    code = normalize_code(code)
    if section not in {"financial", "dividends", "valuation", "shareholders", "financing"}:
        raise ValueError("未知数据类型")
    lock_key = section + ":" + code
    with _DATA_LOCKS.setdefault(lock_key, Lock()):
        if section in {"shareholders", "financing"}:
            return load_chips(code, section, refresh)
        if section == "financial":
            data = load_stock(code, refresh)
        else:
            data = _read_cache(CACHE / f"{code}.json")
            if not data:
                raise ValueError("财报尚未加载，请稍后重试")
            if section == "valuation":
                reports = data.get("reports", [])
                if data.get("report_date_basis") != REPORT_DATE_BASIS:
                    reports = normalize_report_dates(reports)
                return _valuation_payload(load_valuation(code, reports, refresh))
            events, warnings = load_dividends(data, refresh)
            data = _read_cache(CACHE / f"{code}.json") or data
            data = {**data, "dividend_events": events,
                    "warnings": data.get("warnings", []) + warnings}
        return {**_financial_payload(data), "cached_stocks": cached_stocks()}


def render_page(code: str, refresh: bool) -> str:
    """Return cached charts immediately, without making any external requests."""
    payload = {"code": code, "views": {}, "warnings": [], "valuation": {"views": {}},
               "loading": {"financial": True, "dividends": True, "valuation": True},
               "refresh_requested": refresh}
    try:
        code = normalize_code(code)
        data = _read_cache(CACHE / f"{code}.json")
        valuation = _read_cache(VALUATION_CACHE / f"{code}.json")
        payload.update(_financial_payload(data))
        payload["valuation"] = _valuation_payload(valuation)
        payload["loading"] = {"financial": _needs_financial(data),
                              "dividends": _needs_dividends(data),
                              "valuation": not valuation
                              or valuation.get("updated_on") != date.today().isoformat()
                              or valuation.get("basis") != VALUATION_BASIS}
        for section in ("shareholders", "financing"):
            chips = _read_cache(CHIP_CACHE / section / f"{code}.json")
            payload[section] = chip_payload(chips, section)
            payload["loading"][section] = (not chips or chips.get("updated_on") != date.today().isoformat()
                                            or chips.get("basis") != CHIP_BASIS)
        error = ""
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        error = str(exc)
    payload["cached_stocks"] = cached_stocks()
    embedded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    return (TEMPLATE.replace("__PAYLOAD__", embedded)
            .replace("__CODE__", html.escape(code, quote=True))
            .replace("__ERROR__", html.escape(error)))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        status = 200
        if parsed.path == "/plotly.min.js":
            body = get_plotlyjs().encode("utf-8")
            content_type = "text/javascript; charset=utf-8"
        elif parsed.path == "/":
            query = parse_qs(parsed.query)
            code = query.get("code", ["601919"])[0]
            body = render_page(code, query.get("refresh") == ["1"]).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif parsed.path in {"/api/financial", "/api/dividends", "/api/valuation",
                             "/api/shareholders", "/api/financing"}:
            query = parse_qs(parsed.query)
            try:
                data = load_chart_data(query.get("code", ["601919"])[0],
                                       parsed.path.rsplit("/", 1)[-1],
                                       query.get("refresh") == ["1"])
            except (requests.RequestException, ValueError, KeyError, TypeError, OSError) as exc:
                status = 503
                data = {"error": str(exc)}
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if parsed.path == "/plotly.min.js":
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = 8765, open_browser: bool = False):
    # P1 initializes infrastructure only. Business loaders still read/write JSON.
    with instance_lock(DATABASE_PATH):
        address = ("127.0.0.1", port)
        server = ThreadingHTTPServer(address, Handler)
        try:
            db = Database(DATABASE_PATH)
            runtime = db.initialize()
            db.check()
            recovered = db.recover_interrupted_runs()
            db.daily_backup()
            print(f"SQLite {runtime['sqlite_version']} / {runtime['journal_mode']} / "
                  f"结构版本 {runtime['schema_version']}；恢复中断任务 {recovered} 个", flush=True)
            url = f"http://127.0.0.1:{port}/"
            print(f"季度分析页面：{url}", flush=True)
            if open_browser:
                webbrowser.open(url)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
