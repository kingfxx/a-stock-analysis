"""Loopback-only web server and small per-stock quarterly cache."""

from __future__ import annotations

import html
import json
import shutil
import time
import sqlite3
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
from .storage import DEFAULT_DATABASE, Database, StorageError, instance_lock
from .price_service import PriceService, PriceVersionUnavailable, month_closes
from .price_projection import chip_prices, financial_prices, valuation_prices
from .fundamental_service import FundamentalService
from .dividend_service import DividendService
from .valuation_service import ValuationService
from .update_service import ChipService, instrument_id, sync_state, recently_checked, audit_due
from .storage import SyncKey
from .chips import (CHIP_BASIS, chip_payload, chip_rows, fetch_chip_records, missing_chip_history,
                    shareholder_price_snapshots)
from .sources import (fetch_cash_flow_reports, fetch_financial_reports,
                      fetch_stock_name, normalize_code, normalize_report_dates)
from .valuation import (fetch_dividend_events, fetch_industry_snapshot, fetch_valuation_series,
                        merge_adjusted_prices, merge_dividend_yields, monthly_valuation, valuation_summary,
                        monthly_dividend_yields, valuation_summary_observations)


ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "fundamentals"
VALUATION_CACHE = ROOT / "data" / "valuation"
CHIP_CACHE = ROOT / "data" / "chips"
DATABASE_PATH = DEFAULT_DATABASE
_SERVICES, _SERVICE_LOCK = {}, Lock()
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
    if Path(DATABASE_PATH).exists():
        return p4_services()[2].update(code, reports, refresh=refresh)
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
    if Path(DATABASE_PATH).exists():
        return p4_services()[0].update(code, refresh=refresh)
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
    if Path(DATABASE_PATH).exists():
        updated = p4_services()[1].update(data["code"], refresh=refresh)
        return updated["events"], updated["warnings"]
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
    if Path(DATABASE_PATH).exists():
        db = services()[0].db
        with db.connection() as conn:
            return [{"code": row["code"], "name": row["name"]} for row in conn.execute(
                "SELECT code,name FROM instruments ORDER BY code")]
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


def _p4_valuation_payload(data, *, raw=None, qfq=None, events=None):
    """Project saved observations at all display frequencies without source requests."""
    if not data:
        return {"views": {}, "warnings": []}
    raw, qfq, events = raw or [], qfq or [], events or []
    rows = merge_dividend_yields(data.get("rows", []), monthly_dividend_yields(raw, events)) if raw else data.get("rows", [])
    rows = merge_adjusted_prices(rows, month_closes(qfq)) if qfq else rows
    observation_rows = [dict(row) for row in data.get("observation_rows", [])]
    qfq_by_day = {row["date"]: row["close"] for row in qfq}
    for row in observation_rows:
        if row["date"] in qfq_by_day:
            row["qfq_close"] = qfq_by_day[row["date"]]
            row["qfq_close_date"] = row["date"]
    trading_dates = [row["date"] for row in raw]
    views = {}
    for years in (3, 5, 10):
        views[str(years)] = {}
        for metric in ("pe", "pb", "ps"):
            if observation_rows:
                views[str(years)][metric] = valuation_summary_observations(
                    observation_rows, metric, years, data.get("industry", {}), trading_dates=trading_dates)
            else:
                views[str(years)][metric] = valuation_summary(rows, metric, years, data.get("industry", {}))
        yield_summary = valuation_summary(rows, "dividend_yield", years, data.get("industry", {}))
        yield_summary["rows_by_frequency"] = {frequency: yield_summary["rows"] for frequency in ("day", "week", "month")}
        yield_summary["frequency"] = {3: "day", 5: "week", 10: "month"}[years]
        views[str(years)]["dividend_yield"] = yield_summary
    return {"views": views, "updated_on": data.get("updated_on"),
            "peer_count": data.get("industry", {}).get("peer_count"),
            "sampling_version": data.get("basis"),
            "coverage": data.get("coverage", {}),
            "legacy_snapshot_available": data.get("legacy_snapshot_available", False),
            "warnings": data.get("warnings", [])}


def services(*, initialized=False):
    key = (str(Path(DATABASE_PATH).resolve()), str(CHIP_CACHE.resolve()))
    with _SERVICE_LOCK:
        if key not in _SERVICES:
            db = Database(DATABASE_PATH)
            if not initialized:
                with instance_lock(db.path):
                    db.initialize()
            _SERVICES[key] = (ChipService(db, CHIP_CACHE), PriceService(db))
        return _SERVICES[key]


def p4_services():
    db = services()[0].db
    return (FundamentalService(db, CACHE), DividendService(db, CACHE),
            ValuationService(db, VALUATION_CACHE, fetch_industry=ValuationService._fetch_industry))


def import_p4_legacy(db):
    """One-time, dataset-qualified imports; damaged files never block other stocks."""
    importers = ((FundamentalService(db, CACHE), CACHE),
                 (DividendService(db, CACHE), CACHE),
                 (ValuationService(db, VALUATION_CACHE), VALUATION_CACHE))
    warnings = []
    for importer, root in importers:
        for path in sorted(root.glob("*.json")):
            try:
                importer.import_legacy(normalize_code(path.stem))
            except (ValueError, KeyError, TypeError, OSError, StorageError, sqlite3.DatabaseError) as exc:
                warnings.append(f"{path.name} {type(importer).__name__} 导入未完成，原文件保留：{exc}")
    return warnings


def p4_loading(code):
    db = services()[0].db
    identity = instrument_id(db, code)
    if not identity:
        return {"financial": True, "dividends": True, "valuation": True}
    datasets = {"financial": [(f"financial:{kind}", "sina:CompanyFinanceService.getFinanceReport2022")
                              for kind in ("lrb", "fzb", "llb")],
                "dividends": [("dividends", "eastmoney:RPT_SHAREBONUS_DET")],
                "valuation": [(f"valuation:{metric}", "baidu:opendata")
                              for metric in ("pe", "pb", "market_cap")]
                + [("industry", "eastmoney:RPT_PCF10_INDUSTRY_CVALUE")]}
    return {section: any((not (state := sync_state(db, SyncKey(identity, dataset, source)))
                           or not recently_checked(state, seconds=86400) or audit_due(state))
                          for dataset, source in keys)
            for section, keys in datasets.items()}


def fetch_daily_prices(code, session, adjust, earliest_date, latest_date):
    """All production consumers share the same stored daily source history."""
    prices = services()[1]
    result = prices.ensure(code, adjust or "raw")
    if result["warnings"]:
        raise ValueError("；".join(result["warnings"]))
    start = (date.fromisoformat(earliest_date) - timedelta(days=180)).isoformat()
    end = min(date.today(), date.fromisoformat(latest_date) + timedelta(days=180)).isoformat()
    return prices.read(code, adjust or "raw", start=start, end=end)


def fetch_monthly_prices(code, session, adjust="qfq"):
    prices = services()[1]
    result = prices.ensure(code, adjust or "raw")
    if result["warnings"]:
        raise ValueError("；".join(result["warnings"]))
    return month_closes(prices.read(code, adjust or "raw"))


def fetch_dividend_yields(code, session):
    if Path(DATABASE_PATH).exists():
        _, dividends, _ = p4_services()
        raw = services()[1].read(code, "raw")
        return monthly_dividend_yields(raw, dividends.read(code))
    events = fetch_dividend_events(code, session)
    end = date.today()
    start = (end - timedelta(days=3660)).isoformat()
    return monthly_dividend_yields(fetch_daily_prices(code, session, "", start, end.isoformat()), events)


def shared_projection(code, version=None, *, lease=False):
    chips, prices = services()
    version = prices.current_version(code) if version is None else version
    raw = prices.read(code, "raw")
    qfq = prices.read(code, "qfq", version=version or 0, lease=lease)
    return version, raw, qfq


def load_chips(code: str, section: str, refresh: bool = False, *, full=False, price_version=None) -> dict:
    chips, prices = services()
    data = chips.update(code, section, refresh=refresh, full=full)
    version, raw, qfq = shared_projection(code, price_version, lease=True)
    return {**chip_prices(data, section, raw, qfq, version),
            "price_validated_at": prices.version_info(code, version).get("validated_at")}


def price_bundle(code, *, refresh=False, full=False, version=None):
    code = normalize_code(code)
    chips, prices = services()
    warnings = []
    if version is None:
        for adjustment in ("raw", "qfq"):
            try:
                warnings += prices.ensure(code, adjustment, refresh=refresh, full=full)["warnings"]
            except ValueError as exc:
                warnings.append(str(exc))
    version, raw, qfq = shared_projection(code, version, lease=True)
    fundamentals, dividends, valuations = p4_services()
    financial = fundamentals.read(code)
    events = dividends.read(code)
    financial["dividend_events"] = events
    financial["dividend_basis"] = DIVIDEND_BASIS if events else None
    valuation = valuations.read(code, financial.get("reports", []))
    if not version:
        warnings.append("共享前复权尚未就绪，财务和估值的已有价格快照暂按缓存展示。")
    price_meta = prices.version_info(code, version)
    holders = chip_prices(chips.cached(code, "shareholders"), "shareholders", raw, qfq, version)
    financing = chip_prices(chips.cached(code, "financing"), "financing", raw, qfq, version)
    for data in (holders, financing):
        data["price_validated_at"] = price_meta.get("validated_at")
    return dict(price_version=version, price_context=price_meta, warnings=warnings,
                financial=_financial_payload(financial_prices(financial, raw, qfq) if version else financial),
                valuation=_p4_valuation_payload(valuation, raw=raw, qfq=qfq, events=events),
                shareholders=holders, financing=financing)


def load_chart_data(code: str, section: str, refresh: bool = False, *, price_version=None, full=False) -> dict:
    """Network work runs in separate requests; serialize writes to each stock cache."""
    code = normalize_code(code)
    if section not in {"financial", "dividends", "valuation", "shareholders", "financing"}:
        raise ValueError("未知数据类型")
    lock_key = section + ":" + code
    with _DATA_LOCKS.setdefault(lock_key, Lock()):
        if section in {"shareholders", "financing"}:
            return load_chips(code, section, refresh, price_version=price_version, full=full)
        if Path(DATABASE_PATH).exists():
            fundamentals, dividends, valuations = p4_services()
            if section == "financial":
                data = fundamentals.update(code, refresh=refresh, full=full)
                data["dividend_events"] = dividends.read(code)
                data["dividend_basis"] = DIVIDEND_BASIS if data["dividend_events"] else None
                if price_version:
                    _, raw, qfq = shared_projection(code, price_version, lease=True)
                    data = financial_prices(data, raw, qfq)
                return {**_financial_payload(data), "cached_stocks": cached_stocks(),
                        "price_version": price_version}
            if section == "dividends":
                data = fundamentals.read(code)
                updated = dividends.update(code, refresh=refresh, full=full)
                data["dividend_events"] = updated["events"]
                data["dividend_basis"] = DIVIDEND_BASIS if updated["events"] else None
                data["warnings"] += updated["warnings"]
                if price_version:
                    _, raw, qfq = shared_projection(code, price_version, lease=True)
                    data = financial_prices(data, raw, qfq)
                return {**_financial_payload(data), "cached_stocks": cached_stocks(),
                        "price_version": price_version}
            reports = fundamentals.read(code).get("reports", [])
            result = valuations.update(code, reports, refresh=refresh, full=full)
            _, raw, qfq = shared_projection(code, price_version, lease=True)
            result["warnings"] += []
            return {**_p4_valuation_payload(result, raw=raw, qfq=qfq, events=dividends.read(code)),
                    "price_version": price_version}
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
                valuation = load_valuation(code, reports, refresh)
                if price_version:
                    _, _, qfq = shared_projection(code, price_version, lease=True)
                    valuation = valuation_prices(valuation, qfq)
                return {**_valuation_payload(valuation), "price_version": price_version}
            events, warnings = load_dividends(data, refresh)
            data = _read_cache(CACHE / f"{code}.json") or data
            data = {**data, "dividend_events": events,
                    "warnings": data.get("warnings", []) + warnings}
        if price_version:
            _, raw, qfq = shared_projection(code, price_version, lease=True)
            data = financial_prices(data, raw, qfq)
        return {**_financial_payload(data), "cached_stocks": cached_stocks(), "price_version": price_version}


def _consumer_cache(path, warnings):
    """One damaged legacy consumer must not hide independent SQLite facts."""
    try:
        data = _read_cache(path)
        if not isinstance(data, dict):
            raise ValueError("缓存不是对象")
        return data
    except (ValueError, OSError) as exc:
        warnings.append(f"{path.parent.name} 缓存不可用，原文件保留：{exc}")
        return {}


def render_page(code: str, refresh: bool) -> str:
    """Return cached charts immediately, without making any external requests."""
    payload = {"code": code, "views": {}, "warnings": [], "valuation": {"views": {}},
               "loading": {"financial": True, "dividends": True, "valuation": True},
               "refresh_requested": refresh, "price_version": None, "price_needs_update": True}
    cache_warnings = []
    try:
        code = normalize_code(code)
        if Path(DATABASE_PATH).exists():
            fundamentals, dividends, valuations = p4_services()
            data = fundamentals.read(code)
            data["dividend_events"] = dividends.read(code)
            data["dividend_basis"] = DIVIDEND_BASIS if data["dividend_events"] else None
            valuation = valuations.read(code, data.get("reports", []))
        else:
            data = _consumer_cache(CACHE / f"{code}.json", cache_warnings)
            valuation = _consumer_cache(VALUATION_CACHE / f"{code}.json", cache_warnings)
        payload.update(_financial_payload(data))
        payload["valuation"] = (_p4_valuation_payload(valuation, events=data.get("dividend_events"))
                                if Path(DATABASE_PATH).exists() else _valuation_payload(valuation))
        payload["loading"] = {"financial": _needs_financial(data),
                              "dividends": _needs_dividends(data),
                              "valuation": not valuation
                              or valuation.get("updated_on") != date.today().isoformat()
                              or valuation.get("basis") not in (VALUATION_BASIS, "baidu_dense_observations_v1")}
        if Path(DATABASE_PATH).exists():
            payload["loading"] = p4_loading(code)
        payload["price_version"] = None
        payload["price_needs_update"] = True
        raw, qfq, sql_chips = [], [], None
        if Path(DATABASE_PATH).exists():
            sql_chips, price_service = services()
            version, raw, qfq = shared_projection(code)
            payload["price_version"] = version
            # Read-only inspection: a page visit never creates stocks or leases.
            identity = instrument_id(sql_chips.db, code)
            if identity:
                payload["price_needs_update"] = any(not recently_checked(sync_state(sql_chips.db,
                    SyncKey(identity, dataset, "tencent", adjustment))) for dataset, adjustment in
                    (("prices_raw", "raw"), ("prices_adjusted", "qfq")))
            normalized = {**data, "reports": normalize_report_dates(data.get("reports", [])),
                          "report_date_basis": REPORT_DATE_BASIS} if data else {}
            if version:
                payload.update(_financial_payload(financial_prices(normalized, raw, qfq)))
                payload["valuation"] = _p4_valuation_payload(valuation, raw=raw, qfq=qfq,
                                                            events=data.get("dividend_events"))
        for section in ("shareholders", "financing"):
            cached = sql_chips.cached(code, section) if sql_chips else {}
            if cached.get("stored_count") or cached.get("empty"):
                payload[section] = chip_prices(cached, section, raw, qfq, payload["price_version"])
                payload["loading"][section] = cached.get("needs_update", True)
            else:
                try:
                    chips = _read_cache(CHIP_CACHE / section / f"{code}.json")
                    fallback = chip_payload(chips, section)
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    chips, fallback = {}, {"rows": [], "warnings": [f"旧缓存不可用：{exc}"]}
                payload[section] = chip_prices(fallback, section, raw, qfq, payload["price_version"])
                payload["loading"][section] = bool(chips) or not cached.get("empty")
        error = "；".join(cache_warnings)
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
                             "/api/shareholders", "/api/financing", "/api/prices"}:
            query = parse_qs(parsed.query)
            try:
                code = query.get("code", ["601919"])[0]
                version_key = "version" if parsed.path == "/api/prices" else "price_version"
                version = int(query[version_key][0]) if version_key in query else None
                if version is not None and version < 0:
                    raise ValueError("价格版本无效")
                if parsed.path == "/api/prices":
                    data = price_bundle(code, refresh=query.get("refresh") == ["1"], full=query.get("full") == ["1"], version=version)
                else:
                    data = load_chart_data(code, parsed.path.rsplit("/", 1)[-1], query.get("refresh") == ["1"],
                                           price_version=version, full=query.get("full") == ["1"])
            except PriceVersionUnavailable as exc:
                status, data = 409, {"error": str(exc), "price_version_expired": True}
            except (requests.RequestException, ValueError, KeyError, TypeError, OSError, StorageError, sqlite3.DatabaseError) as exc:
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
    # Import old snapshots once; normal requests read the SQLite facts.
    with instance_lock(DATABASE_PATH):
        address = ("127.0.0.1", port)
        server = ThreadingHTTPServer(address, Handler)
        try:
            db = Database(DATABASE_PATH)
            runtime = db.initialize()
            db.check()
            recovered = db.recover_interrupted_runs()
            chips, _ = services(initialized=True)
            for section in ("financing", "shareholders"):
                for path in sorted((CHIP_CACHE / section).glob("*.json")):
                    try:
                        chips.import_legacy(normalize_code(path.stem), section)
                    except (ValueError, OSError, StorageError, sqlite3.DatabaseError) as exc:
                        print(f"{path.name} {section} 迁移未完成，原文件保留：{exc}", flush=True)
            for warning in import_p4_legacy(db):
                print(warning, flush=True)
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
