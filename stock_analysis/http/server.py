"""Loopback-only web server and small per-stock quarterly cache."""

from __future__ import annotations

from stock_analysis.paths import PROJECT_ROOT
from stock_analysis.runtime import run_server
from stock_analysis.http.assets import asset_path
from stock_analysis.http.rendering import research_sidebar as render_research_sidebar
import html
import hashlib
import json
import shutil
import time
import sqlite3
import secrets
import subprocess
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import parse_qs, urlparse, unquote, urlencode

import requests
from plotly.offline import get_plotlyjs

from stock_analysis.domains.financials.core import build_period_rows, disclosure_reference_snapshots, disclosure_snapshots, period_end_snapshots, view_rows
from stock_analysis.infrastructure.network import create_data_session
from stock_analysis.infrastructure.storage import DEFAULT_DATABASE, Database, StorageError, instance_lock
from stock_analysis.domains.stocks.price_service import PriceService, PriceVersionUnavailable, month_closes
from stock_analysis.domains.stocks.price_projection import chip_prices, financial_prices, valuation_prices
from stock_analysis.domains.financials.fundamental_service import FundamentalService
from stock_analysis.domains.stocks.announcement_service import AnnouncementService
from stock_analysis.domains.financials.business_segment_service import BusinessSegmentService
from stock_analysis.domains.stocks.disclosure_dates import is_earnings_notice
from stock_analysis.domains.financials.statements import read_statements
from stock_analysis.domains.valuation.dcf import read_dcf, build_auto_wacc
from stock_analysis.domains.valuation.dividend_forecast import read_dividend_forecast
from stock_analysis.domains.valuation.institution_forecast import fetch_institution_forecast
from stock_analysis.domains.industries.industry_service import IndustryService
from stock_analysis.maintenance.maintenance import MaintenanceService
from stock_analysis.domains.indices.index_collection import start_operation, run_operation, cancel_operation, recover_operations
from stock_analysis.domains.indices.index_repository import read_indices
from stock_analysis.domains.indices.index_read_service import IndexReadService
from stock_analysis.domains.market import market_congestion
from stock_analysis.collection.collection_management import CollectionManagement
from stock_analysis.collection.collection import ProviderBlocked, read_source
from stock_analysis.maintenance.database_restore import RestoreManager, RequestGate, MAX_UPLOAD
from stock_analysis.domains.stocks.dividend_service import DividendService
from stock_analysis.domains.valuation.valuation_service import ValuationService
from stock_analysis.domains.stocks.sync_support import instrument_id, sync_state, recently_checked, checked_today, audit_due, backup_before_update, recent_sync_failure
from stock_analysis.domains.stocks.update_service import ChipService
from stock_analysis.infrastructure.storage import SyncKey
from stock_analysis.domains.stocks.stock_collection import (begin_stock_batch, complete_stock_batch, fail_stock_batch,
                               recent_failed_stock_batch, routed_stock_requests)
from stock_analysis.domains.stocks.stock_library import StockLibrary
from stock_analysis.maintenance.stock_cleanup import StockCleanupService
from stock_analysis.domains.research.assessment.analysis_service import AnalysisService
from stock_analysis.domains.research.assessment.checklist_snapshot import capture as checklist_capture
from stock_analysis.domains.research.assessment.checklist_validation import prompt as checklist_prompt, validate_output as validate_checklist, OUTPUT_VERSION
from stock_analysis.domains.research.reports.company_report_service import CompanyReportService
from stock_analysis.domains.research.reports.research_reports import list_reports, resolve_report
from stock_analysis.domains.valuation.valuation_transport import compact_valuation
from stock_analysis.domains.notes.investment_notes import InvestmentNotes, NoteConflict, MAX_DOCUMENT
from stock_analysis.domains.notes.note_snapshots import catalog as note_catalog
from stock_analysis.domains.research.ai.ai_provider import ChatGPTProvider, ProviderError
from stock_analysis.domains.stocks.chips import (CHIP_BASIS, chip_payload, chip_rows, fetch_chip_records, missing_chip_history,
                    shareholder_price_snapshots)
from stock_analysis.domains.stocks.sources import (fetch_cash_flow_reports, fetch_financial_reports,
                      fetch_stock_name, normalize_code, normalize_report_dates)
from stock_analysis.domains.valuation.valuation import (fetch_dividend_events, fetch_industry_snapshot, fetch_valuation_series,
                        merge_adjusted_prices, merge_dividend_yields, monthly_valuation, valuation_summary,
                        aggregate_valuation_rows, cash_dividend_per_share_365,
                        daily_dividend_yield_observations, monthly_dividend_yields,
                        valuation_summary_observations)


ROOT = PROJECT_ROOT
CACHE = ROOT / "data" / "fundamentals"
VALUATION_CACHE = ROOT / "data" / "valuation"
CHIP_CACHE = ROOT / "data" / "chips"
DATABASE_PATH = DEFAULT_DATABASE
_SERVICES, _SERVICE_LOCK = {}, Lock()
_AI_SERVICES = {}
_INDUSTRY_SERVICES = {}
_MAINTENANCE_SERVICES = {}
LOCAL_SESSION_TOKEN = secrets.token_urlsafe(32)
TEMPLATE = (asset_path('index.html')).read_text(encoding="utf-8")
RESEARCH_SIDEBAR_TEMPLATE = (asset_path('research-sidebar.html')).read_text(encoding="utf-8")
CHECKLIST_DISPLAY_VERSION = hashlib.sha256((asset_path('ai-checklist.js')).read_bytes()).hexdigest()[:12]
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


def research_sidebar(page: str) -> str:
    return render_research_sidebar(page, RESEARCH_SIDEBAR_TEMPLATE)


FINANCIAL_VALUES = ("revenue_ytd", "profit_ytd", "shares", "equity") + NEW_REPORT_FIELDS
CASH_VALUES = ("operating_cash_flow_ytd", "main_business_cash_flow_ytd", "capex_ytd")


def industry_service():
    with _SERVICE_LOCK:
        key = str(Path(DATABASE_PATH).resolve())
        if key not in _INDUSTRY_SERVICES:
            db = Database(DATABASE_PATH)
            db.initialize()
            _INDUSTRY_SERVICES[key] = IndustryService(db)
        return _INDUSTRY_SERVICES[key]


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
    if not (data.get("name") or "").strip():
        try:
            name = fetch_stock_name(data["code"], session)
        except (requests.RequestException, ValueError, KeyError):
            name = None
        data = {**data, "name": name}
        _save_cache(path, data)
    return data


def _add_sqlite_missing_name(db: Database, data: dict, *, refresh=False) -> dict:
    if (data.get("name") or "").strip():
        return data
    if not refresh and recent_failed_stock_batch(db, "instrument_name", data["code"]):
        return {**data, "warnings": [*data.get("warnings", []), "股票名称最近自动获取失败，暂缓重试"]}
    identity = instrument_id(db, data["code"])
    with db.connection() as conn:
        row = conn.execute("SELECT id FROM sync_runs WHERE instrument_id=? ORDER BY id DESC LIMIT 1",
                           (identity,)).fetchone() if identity else None
    if not row:
        return {**data, "warnings": [*data.get("warnings", []), "股票名称暂缺，未建立无关联的外部请求"]}
    run_id = row["id"]
    batch_id = begin_stock_batch(db, run_id, "tencent", "instrument_name", data["code"],
                                 trigger="manual_refresh" if refresh else "automatic", scope={"source_interface": "qt.gtimg.cn",
                                                              "related_to_dataset": "financial"},
                                 parser_version="tencent_quote_v1", definition_version="instrument_name_v1")
    try:
        with routed_stock_requests(db, batch_id, "tencent", "instrument_name"):
            with create_data_session() as session:
                name = fetch_stock_name(data["code"], session)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("名称响应为空")
    except (requests.RequestException, ValueError, KeyError) as exc:
        fail_stock_batch(db, batch_id, exc)
        return {**data, "warnings": [*data.get("warnings", []), f"股票名称获取失败，暂可按代码查找：{exc}"]}
    except RuntimeError as exc:
        fail_stock_batch(db, batch_id, exc)
        return {**data, "warnings": [*data.get("warnings", []), f"股票名称获取失败，暂可按代码查找：{exc}"]}
    complete_stock_batch(db, batch_id, result={"sync_run_id": run_id, "name_fetched": True})
    backup_before_update(db)
    db.ensure_instrument(data["code"], name.strip())
    return {**data, "name": name.strip()}


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
            if key == "raw":
                prices["raw_period_end"] = period_end_snapshots(reports, daily)
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
                "SELECT code,name FROM instruments WHERE id NOT IN (SELECT instrument_id FROM stock_unfollowed) ORDER BY code")]
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
    return (not data or not (data.get("name") or "").strip()
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
                             data.get("dividend_events") or None,
                             period_end_prices=prices.get("raw_period_end"))
    return {"code": data["code"], "name": data.get("name"), "updated_at": data.get("updated_at"),
            "views": {name: view_rows(rows, name) for name in ("quarter", "ytd", "year", "ttm")},
            "warnings": data.get("warnings", []), "needs_dividends": _needs_dividends(data),
            "announcement_sync": data.get("announcement_sync", {}),
            "business_segment_sync": data.get("business_segment_sync", {})}


def _financial_with_announcement_state(data, db):
    if data and data.get("code"):
        sync = AnnouncementService(db).read(data["code"]).get("sync", {})
        data = {**data, "announcement_sync": sync}
        data['business_segment_sync'] = BusinessSegmentService(db).read(data['code']).get('sync', {})
    return data


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
        daily_yield_rows = daily_dividend_yield_observations(raw, events) if raw else None
        yield_summary = valuation_summary(rows, "dividend_yield", years, data.get("industry", {}),
                                          sample_rows=daily_yield_rows or None)
        sample_frequency = "day" if daily_yield_rows else "month"
        yield_summary["sample_frequency"] = sample_frequency
        yield_summary["percentile_frequency"] = sample_frequency
        chart_rows = daily_yield_rows if daily_yield_rows else [
            row for row in rows if row.get("dividend_yield") is not None
        ]
        for row in chart_rows:
            if row.get("cash_dividend_per_share_365") is None:
                observation_date = row.get("dividend_yield_date") or row["date"]
                row["cash_dividend_per_share_365"] = cash_dividend_per_share_365(events, observation_date)
        end = date.today()
        try:
            start = end.replace(year=end.year - years)
        except ValueError:
            start = end.replace(year=end.year - years, day=28)
        chart_rows = [row for row in chart_rows if start.isoformat() <= row["date"] <= end.isoformat()]
        yield_summary["rows_by_frequency"] = {
            frequency: aggregate_valuation_rows(chart_rows, frequency, as_of=end.isoformat())
            for frequency in ("day", "week", "month")
        }
        yield_summary["frequency"] = {3: "day", 5: "week", 10: "month"}[years]
        yield_summary["rows"] = yield_summary["rows_by_frequency"][yield_summary["frequency"]]
        views[str(years)]["dividend_yield"] = yield_summary
    if raw or qfq:
        def price_period(day, frequency):
            if frequency == "week":
                day = date.fromisoformat(day)
                return (day - timedelta(days=day.weekday())).isoformat()
            return day[:7] if frequency == "month" else day
        trading_periods = {frequency: {price_period(day, frequency) for day in trading_dates}
                           for frequency in ("day", "week", "month")}
        price_dates = sorted({row["date"] for row in raw + qfq if row["date"] <= date.today().isoformat()})
        last_trades = {frequency: {price_period(day, frequency): day for day in price_dates}
                       for frequency in ("day", "week", "month")}
        for metrics in views.values():
            for metric, summary in metrics.items():
                for frequency, items in summary.get("rows_by_frequency", {}).items():
                    projected = []
                    for row in items:
                        point = dict(row)
                        period = price_period(row["date"], frequency)
                        trade_day = last_trades[frequency].get(period)
                        point["qfq_close"] = qfq_by_day.get(trade_day)
                        point["qfq_close_date"] = trade_day if point["qfq_close"] is not None else None
                        if trading_dates:
                            point["qfq_no_trades"] = period not in trading_periods[frequency]
                        projected.append(point)
                    summary["rows_by_frequency"][frequency] = projected
                if summary.get("frequency"):
                    summary["rows"] = summary["rows_by_frequency"][summary["frequency"]]
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
                              for kind in ("lrb", "fzb", "llb")]
                             + [("business_segments", "eastmoney:RPT_F10_FN_MAINOP")],
                "dividends": [("dividends", "eastmoney:RPT_SHAREBONUS_DET")],
                "valuation": [(f"valuation:{metric}", "baidu:opendata")
                              for metric in ("pe", "pb", "market_cap")]
                + [("industry", "eastmoney:RPT_PCF10_INDUSTRY_CVALUE")]}
    loading = {section: any((dataset != 'business_segments' or not recent_sync_failure(
                               db, SyncKey(identity, dataset, source))) and
                           (not (state := sync_state(db, SyncKey(identity, dataset, source)))
                            or not recently_checked(state, seconds=86400) or audit_due(state, dataset))
                          for dataset, source in keys)
            for section, keys in datasets.items()}
    with db.connection() as conn:
        name = conn.execute("SELECT name FROM instruments WHERE id=?", (identity,)).fetchone()[0]
    loading["financial"] = loading["financial"] or not (name or "").strip()
    return loading


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
    financial = _financial_with_announcement_state(fundamentals.read(code), fundamentals.db)
    events = dividends.read(code)
    financial["dividend_events"] = events
    financial["dividend_basis"] = DIVIDEND_BASIS if events else None
    valuation = valuations.read(code, financial.get("reports", []))
    if not version:
        warnings.append("共享前复权尚未就绪，前复权价格留空；披露日估算市值独立使用已有未复权价格。")
    price_meta = prices.version_info(code, version)
    holders = chip_prices(chips.cached(code, "shareholders"), "shareholders", raw, qfq, version)
    financing = chip_prices(chips.cached(code, "financing"), "financing", raw, qfq, version)
    for data in (holders, financing):
        data["price_validated_at"] = price_meta.get("validated_at")
    return dict(price_version=version, price_context=price_meta, warnings=warnings,
                financial=_financial_payload(financial_prices(financial, raw, qfq)),
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
                try:
                    data = fundamentals.update(code, refresh=refresh, full=full)
                except (ValueError, OSError, StorageError, sqlite3.DatabaseError, requests.RequestException) as exc:
                    data = fundamentals.read(code)
                    data.setdefault('warnings', []).append(f'财务同步失败，保留缓存：{exc}')
                try:
                    segments = BusinessSegmentService(fundamentals.db).update(code, refresh=refresh, full=full)
                except (ValueError, OSError, StorageError, sqlite3.DatabaseError, requests.RequestException) as exc:
                    segments = {'sync': {'status': 'failed', 'error': str(exc)},
                                'warnings': [f'主营构成独立同步失败，保留财报结果：{exc}']}
                data.setdefault('warnings', []).extend(segments.get('warnings', []) if segments.get('sync', {}).get('status') == 'failed' else [])
                financial_warnings = list(data.get("warnings", []))
                if data.get("reports"):
                    try:
                        announcement = AnnouncementService(fundamentals.db).update(
                            code, data["reports"], refresh=refresh, full=full,
                            # First financial sync also bootstraps announcement history for a new stock.
                            allow_initial=True)
                    except (ProviderBlocked, ValueError, KeyError, TypeError, OSError,
                            requests.RequestException, StorageError, sqlite3.DatabaseError) as exc:
                        try:
                            old_sync = AnnouncementService(fundamentals.db).read(code).get("sync", {})
                        except (ValueError, OSError, StorageError, sqlite3.DatabaseError):
                            old_sync = {}
                        announcement = {"sync": {**old_sync, "error": str(exc)},
                                         "warnings": [f"公告日期独立同步失败，已保留新浪财务事实：{exc}"]}
                    if announcement.get("sync", {}).get("status") == "failed":
                        announcement.setdefault("warnings", []).append("公告日期同步失败，已保留已有判断。")
                    data = fundamentals.read(code)
                    data["warnings"] = list(dict.fromkeys(financial_warnings + data.get("warnings", []) +
                                                           announcement.get("warnings", [])))
                    data["announcement_sync"] = announcement.get("sync", {})
                else:
                    data = _financial_with_announcement_state(data, fundamentals.db)
                data = _add_sqlite_missing_name(fundamentals.db, data, refresh=refresh or full)
                data['business_segment_sync'] = segments.get('sync', {})
                data["dividend_events"] = dividends.read(code)
                data["dividend_basis"] = DIVIDEND_BASIS if data["dividend_events"] else None
                if price_version is not None or services()[1].read(code, "raw"):
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
                if price_version is not None or services()[1].read(code, "raw"):
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
            data = _financial_with_announcement_state(fundamentals.read(code), fundamentals.db)
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
                payload["price_needs_update"] = any(not checked_today(state := sync_state(sql_chips.db,
                    SyncKey(identity, dataset, "tencent", adjustment))) or audit_due(state, dataset) for dataset, adjustment in
                    (("prices_raw", "raw"), ("prices_adjusted", "qfq")))
            normalized = ({**data, "reports": (data.get("reports", [])
                              if data.get("report_date_basis") == REPORT_DATE_BASIS
                              else normalize_report_dates(data.get("reports", []))),
                          "report_date_basis": REPORT_DATE_BASIS} if data else {})
            if version or raw:
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
    payload["stock_library"] = StockLibrary(services()[0].db).read() if Path(DATABASE_PATH).exists() else {}
    payload["unfollowed"] = (StockLibrary(services()[0].db).is_unfollowed(code) if Path(DATABASE_PATH).exists() else False)
    payload['valuation'] = compact_valuation(payload['valuation'])
    embedded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    return (TEMPLATE.replace("__RESEARCH_SIDEBAR__", research_sidebar("stock"))
            .replace("__PAYLOAD__", embedded)
            .replace("__CODE__", html.escape(code, quote=True))
            .replace("__ERROR__", html.escape(error)))


def ai_service():
    key = str(Path(DATABASE_PATH).resolve())
    with _SERVICE_LOCK:
        if key not in _AI_SERVICES:
            db = Database(DATABASE_PATH)
            _AI_SERVICES[key] = AnalysisService(db, ChatGPTProvider(db.path.parent / "ai-private"), snapshotter=checklist_capture, prompt_factory=checklist_prompt, validator=validate_checklist, output_version=OUTPUT_VERSION)
        return _AI_SERVICES[key]


def business_service():
    shared = ai_service()
    key = str(Path(DATABASE_PATH).resolve()) + ':business'
    with _SERVICE_LOCK:
        if key not in _AI_SERVICES:
            from stock_analysis.domains.research.assessment import business_judgment as business
            _AI_SERVICES[key] = AnalysisService(shared.db, shared.provider, snapshotter=business.capture,
                prompt_factory=business.prompt, validator=business.validate, output_version=business.OUTPUT_VERSION, shared=shared)
        return _AI_SERVICES[key]


class Handler(BaseHTTPRequestHandler):
    def _notes_service(self):
        with _SERVICE_LOCK:
            if not hasattr(self.server, 'investment_notes'):
                self.server.investment_notes = InvestmentNotes(Path(DATABASE_PATH).parent)
        return self.server.investment_notes

    def _notes_request(self, parsed, *, write=False):
        try:
            self._ai_host()
            service = self._notes_service()
            query = parse_qs(parsed.query)
            path = parsed.path
            if write:
                self._maintenance_auth()
                size = int(self.headers.get('Content-Length', '0'))
                if self.headers.get_content_type() != 'application/json' or not 0 < size <= MAX_DOCUMENT * 4:
                    raise ValueError('笔记请求格式或大小无效')
                command = json.loads(self.rfile.read(size).decode('utf-8'))
                if not isinstance(command, dict):
                    raise ValueError('笔记请求须为对象')
                if 'code' in command and not isinstance(command['code'], str):
                    raise ValueError('股票代码须为字符串')
                if path == '/api/notes/document':
                    data = service.save(command)
                elif path == '/api/notes/snapshot':
                    data = note_catalog(Database(DATABASE_PATH), command, CACHE, VALUATION_CACHE, CHIP_CACHE)
                elif path == '/api/notes/settings/check':
                    data = {'directory': str(service.check_directory(command.get('directory', '')))}
                elif path == '/api/notes/settings/choose':
                    data = service.choose_directory()
                elif path == '/api/notes/settings/preview':
                    data = service.preview_directory(command)
                elif path == '/api/notes/settings/apply':
                    data = service.apply_directory(command)
                elif path == '/api/notes/associate':
                    data = service.associate(command)
                else:
                    self.send_error(404); return
            elif path == '/api/notes/settings':
                data = {**service.settings(), 'session_token': LOCAL_SESSION_TOKEN}
            elif path == '/api/notes':
                data = {'items': service.listing(), 'directory_version': service.directory_version()}
            elif path == '/api/notes/document':
                data = service.read(query.get('code', [''])[0], query.get('name', [''])[0])
            elif path == '/api/notes/resource':
                body, mime = service.resource(query.get('path', [''])[0])
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'none'; sandbox")
                self.send_header('Cache-Control', 'no-store')
                self.end_headers(); self.wfile.write(body); return
            else:
                self.send_error(404); return
            self._ai_reply(data)
        except NoteConflict as exc:
            self._ai_reply({'error': str(exc), 'conflict': True, 'current': exc.current}, 409)
        except (ValueError, TypeError, KeyError, UnicodeError, OSError, StorageError, sqlite3.DatabaseError, subprocess.TimeoutExpired) as exc:
            self._ai_reply({'error': str(exc)}, 400)

    def _maintenance_auth(self):
        self._ai_host()
        allowed={f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'}
        if self.headers.get('Origin') not in allowed or not secrets.compare_digest(
                self.headers.get('X-Local-Session',''),LOCAL_SESSION_TOKEN):
            raise ValueError('拒绝跨站或无本地会话的维护操作，请刷新页面')

    def _cleanup_service(self):
        with _SERVICE_LOCK:
            if not hasattr(self.server, 'stock_cleanup'):
                self.server.stock_cleanup = StockCleanupService(Database(DATABASE_PATH), activity_check=_runtime_worker_activity)
        return self.server.stock_cleanup

    def _cleanup_post(self, path):
        try:
            self._maintenance_auth()
            size = int(self.headers.get('Content-Length','0'))
            if self.headers.get_content_type()!='application/json' or not 0<size<=8192:
                raise ValueError('清理请求格式无效')
            command = json.loads(self.rfile.read(size).decode('utf-8'))
            service = self._cleanup_service()
            if path=='/api/maintenance/compact':
                data=service.compact(command,self.server.request_gate)
                with _SERVICE_LOCK:
                    _MAINTENANCE_SERVICES.clear()
            elif path.endswith('/preview'):
                data = service.preview(command)
            else:
                data = service.execute(command, self.server.request_gate)
                with _SERVICE_LOCK:
                    _MAINTENANCE_SERVICES.clear()
            self._ai_reply(data)
        except (ValueError,TypeError,UnicodeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
            self._ai_reply({'error':str(exc)},400)

    def _maintenance_post(self, path):
        scheduled=False
        try:
            self._maintenance_auth()
            manager=self.server.restore_manager
            size=int(self.headers.get('Content-Length','0'))
            if path=='/api/maintenance/restore/upload':
                if self.headers.get_content_type()!='application/octet-stream' or not 16<=size<=MAX_UPLOAD:
                    raise ValueError('请选择完整 SQLite 备份文件，最大 4 GiB')
                self.connection.settimeout(300)
                data=manager.upload(self.rfile,size,unquote(self.headers.get('X-Backup-Name','backup.sqlite3')))
            else:
                if self.headers.get_content_type()!='application/json' or not 0<size<=8192:
                    raise ValueError('恢复请求格式无效')
                command=json.loads(self.rfile.read(size).decode('utf-8'))
                if not isinstance(command,dict):raise ValueError('恢复请求必须是对象')
                if path=='/api/maintenance/restore/preview':data=manager.prepare(command)
                elif path=='/api/maintenance/restore/cancel':
                    if command:raise ValueError('取消请求格式无效')
                    data=manager.cancel()
                elif path=='/api/maintenance/restore/confirm':
                    data=manager.schedule(command,self.server.request_gate);scheduled=True
                else:raise ValueError('恢复接口不存在')
            self._ai_reply(data,202 if scheduled else 200)
        except (ValueError,TypeError,UnicodeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
            self._ai_reply({'error':str(exc)},400)
        finally:
            if scheduled:Thread(target=self.server.shutdown,name='database-restore-shutdown',daemon=True).start()

    def _dispatch(self, method):
        gate=getattr(self.server,'request_gate',None)
        # The status response is memory-only and remains available while draining.
        if method=='GET' and urlparse(self.path).path=='/api/maintenance/restore/status':
            try:self._ai_host();self._ai_reply(self.server.restore_manager.state)
            except ValueError as exc:self._ai_reply({'error':str(exc)},400)
            return
        if gate and not gate.enter():
            self._ai_reply({'error':'后台维护正在执行，请稍候','restoring':True},503);return
        try:getattr(self,'_do_'+method)()
        finally:
            if gate:gate.leave()

    def do_POST(self):self._dispatch('POST')
    def do_GET(self):self._dispatch('GET')
    def do_PUT(self):
        if urlparse(self.path).path != '/api/notes/document':
            self.send_error(405); return
        self._dispatch('POST')

    def log_message(self, format, *args):
        # OAuth callbacks contain codes; never log their URL or query parameters.
        if urlparse(self.path).path == "/auth/callback":
            return
        super().log_message(format, *args)

    def _ai_reply(self, data, status=200):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # A rapid index switch can abort the previous browser fetch.
            return

    def _ai_host(self):
        port = self.server.server_port
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            raise ValueError("不允许此 Host 访问本地 AI 接口")

    def _ai_get(self, parsed):
        try:
            self._ai_host()
            service = business_service() if parsed.path.startswith('/api/business-judgment') else ai_service()
            parsed = parsed._replace(path=parsed.path.replace('/api/business-judgment', '/api/analysis', 1))
            query = parse_qs(parsed.query)
            if parsed.path == "/api/ai/status":
                data = {**service.status(), "session_token": LOCAL_SESSION_TOKEN}
            elif parsed.path == "/api/ai/models":
                data = {"models": service.provider.models()}
            elif parsed.path == "/api/analysis":
                data = service.overview(query.get("code", [""])[0])
                data['display_version'] = CHECKLIST_DISPLAY_VERSION
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
                    self.headers.get("X-Local-Session", ""), LOCAL_SESSION_TOKEN):
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
            service = business_service() if business else ai_service()
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
            ai_service().provider.callback({key: values[0] for key, values in query.items()})
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

    def _do_POST(self):
        path = urlparse(self.path).path
        if path in {'/api/shipping-forecast/save','/api/shipping-forecast/prepare-report','/api/shipping-forecast/delete'}:
            try:
                self._ai_host()
                size=int(self.headers.get('Content-Length','0'))
                if self.headers.get_content_type()!='application/json' or not 0<size<=32768:
                    raise ValueError('请提交有效JSON')
                # Drain the bounded body before closing an unauthorized request;
                # unread inbound bytes can reset the connection before 403 arrives.
                body=self.rfile.read(size)
                if self.headers.get('Origin') not in {f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'} or not secrets.compare_digest(self.headers.get('X-Local-Session',''),LOCAL_SESSION_TOKEN):
                    self._ai_reply({'error':'拒绝跨站或缺少本地会话的保存'},403);return
                from stock_analysis.domains.research.sectors.shipping.shipping_forecast import save_version, delete_version
                command=json.loads(body)
                if path.endswith('/delete'):
                    if not isinstance(command,dict) or set(command)!={'id'}:
                        raise ValueError('删除版本请求无效')
                    self._ai_reply(delete_version(Database(DATABASE_PATH),command['id']))
                elif path.endswith('/prepare-report'):
                    from stock_analysis.domains.research.sectors.shipping.shipping_report_metrics import prepare_shipping_report
                    if not isinstance(command,dict) or set(command)!={'period'} or not isinstance(command['period'],str):raise ValueError('请指定单一报告期')
                    self._ai_reply(prepare_shipping_report(Database(DATABASE_PATH),command['period']))
                else:
                    self._ai_reply(save_version(Database(DATABASE_PATH),command))
            except (ValueError,StorageError) as exc:self._ai_reply({'error':str(exc)},400)
            except (OSError,RuntimeError,sqlite3.DatabaseError) as exc:self._ai_reply({'error':str(exc)},503)
            return
        if path.startswith('/api/notes/'):
            self._notes_request(urlparse(self.path), write=True); return
        if path in {'/api/maintenance/compact','/api/maintenance/stock-cleanup/preview','/api/maintenance/stock-cleanup/execute'}:
            self._cleanup_post(path);return
        if path in {'/api/maintenance/restore/upload','/api/maintenance/restore/preview','/api/maintenance/restore/confirm','/api/maintenance/restore/cancel'}:
            self._maintenance_post(path);return
        if path.startswith("/api/ai/") or path == "/api/analysis" or path.startswith("/api/analysis/") or path == '/api/business-judgment' or path.startswith('/api/business-judgment/'):
            self._ai_post(path)
            return
        if path not in {"/api/stock-groups", "/api/maintenance/unfollowed", "/api/industry/refresh", "/api/indices/collect", "/api/indices/cancel", "/api/indices/congestion/schedule", "/api/maintenance/collections/run", "/api/maintenance/collections/cancel", "/api/maintenance/collections/schedule", "/api/maintenance/collections/rate-limit"}:
            self.send_error(404)
            return
        status = 200
        try:
            allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if self.headers.get("Origin") and self.headers["Origin"] not in allowed:
                raise ValueError("不允许跨站修改分组")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("请使用 JSON 操作分组")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536:
                raise ValueError("分组请求大小无效")
            command = json.loads(self.rfile.read(size).decode("utf-8"))
            if path.startswith('/api/maintenance/collections/'):
                if not isinstance(command,dict) or set(command)-{'task_key','params','schedule','provider','settings'}:
                    raise ValueError('采集管理请求无效')
                management=CollectionManagement(Database(DATABASE_PATH),industry_service())
                key=command.get('task_key')
                if path.endswith('/run'):
                    if set(command)-{'task_key','params'}:raise ValueError('运行参数无效')
                    data=management.start(key,command.get('params',{}))
                elif path.endswith('/rate-limit'):
                    if set(command)!={'task_key','provider','settings'}:raise ValueError('限速配置参数无效')
                    data=management.configure_rate(key,command['provider'],command['settings'])
                elif path.endswith('/cancel'):
                    if set(command)!={'task_key'}:raise ValueError('停止参数无效')
                    data=management.cancel(key)
                else:
                    if set(command)!={'task_key','schedule'}:raise ValueError('配置参数无效')
                    data=management.configure(key,command['schedule'])
            elif path=='/api/indices/congestion/schedule':
                data=market_congestion.configure(Database(DATABASE_PATH),command)
            elif path in {'/api/indices/collect','/api/indices/cancel'}:
                if not isinstance(command,dict):
                    raise ValueError('指数采集请求须为对象')
                db=Database(DATABASE_PATH)
                if path.endswith('/cancel'):
                    if command:
                        raise ValueError('取消请求无需参数')
                    cancel_operation(db)
                    data={'cancel_requested':True}
                else:
                    if set(command)-{'mode','providers','start','end','datasets'}:
                        raise ValueError('指数采集参数无效')
                    if 'datasets' in command:
                        if command['datasets']==['market_congestion']:
                            if command.get('providers',['legu'])!=['legu'] or 'start' in command or 'end' in command:
                                raise ValueError('拥挤度任务范围无效')
                            operation=market_congestion.start(db,command.get('mode','incremental'))
                        elif command['datasets']==['bond_zh_us_rate']:
                            if 'start' in command or 'end' in command:
                                raise ValueError('国债收益率由本地水位决定范围')
                            operation=start_operation(db,command.get('mode','incremental'),
                                command.get('providers',['csindex','legu','sws']),datasets=command['datasets'])
                        else:
                            raise ValueError('指数采集数据集无效')
                    else:
                        operation=start_operation(db,command.get('mode','incremental'),command.get('providers',['csindex','legu','sws']),command.get('start'),command.get('end'))
                    Thread(target=run_operation,args=(db,operation),daemon=True).start()
                    data={'operation_id':operation,'status':'running'}
            elif path == '/api/industry/refresh':
                if not isinstance(command,dict) or set(command)-{'action','target','recheck'} or not {'action','target'}<=set(command):
                    raise ValueError('行业更新须指定任务和报告期／季度')
                data = industry_service().start_refresh(command['action'],command['target'],command.get('recheck',False))
            elif path == '/api/maintenance/unfollowed':
                if not isinstance(command, dict) or command.get('action') != 'restore':
                    raise ValueError('此入口仅支持恢复关注')
                library = StockLibrary(services()[0].db)
                data = {'library':library.change(command), 'stocks':library.unfollowed()}
            else:
                data = StockLibrary(services()[0].db).change(command)
        except (ValueError, TypeError, OSError, StorageError, sqlite3.DatabaseError) as exc:
            status, data = 400, {"error": str(exc)}
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/shipping-trend':
            try:
                self._ai_host()
                from stock_analysis.domains.research.sectors.shipping.shipping_forecast import read_trend
                query=parse_qs(parsed.query)
                if query.get('code',['601919'])[0] != '601919': raise ValueError('航运趋势仅适用601919')
                data=read_trend(Database(DATABASE_PATH), index=query.get('index',['CCFI'])[0], frequency=query.get('frequency',['monthly'])[0],
                    route=query.get('route',['综合'])[0], years=query.get('range',['all'])[0], quality=query.get('quality',['official'])[0],
                    profit=query.get('profit',['none'])[0], selected_years=query.get('years',[''])[0].split(',') if query.get('years') else [])
                self._ai_reply(dict(data,session_token=LOCAL_SESSION_TOKEN))
            except (ValueError,StorageError,sqlite3.DatabaseError) as exc:self._ai_reply({'error':str(exc)},400)
            return
        if parsed.path == '/api/shipping-forecast':
            try:
                self._ai_host()
                from stock_analysis.domains.research.sectors.shipping.shipping_forecast import read_forecast, read_version
                query=parse_qs(parsed.query);db=Database(DATABASE_PATH)
                if query.get('code',['601919'])[0]!='601919':raise ValueError('航运预测仅适用601919')
                data=read_version(db,query['version'][0]) if 'version' in query else read_forecast(db,query.get('target',['2026Q3'])[0],int(query.get('weekly_lead_weeks',['2'])[0]))
                self._ai_reply(dict(data,session_token=LOCAL_SESSION_TOKEN))
            except (ValueError,StorageError) as exc:self._ai_reply({'error':str(exc)},400)
            return
        if parsed.path == '/api/notes' or parsed.path.startswith('/api/notes/'):
            self._notes_request(parsed); return
        if parsed.path.startswith('/vendor/notes/'):
            filename = parsed.path.removeprefix('/vendor/notes/')
            if filename not in {'codemirror.js', 'markdown-mode.js', 'markdown-it.js', 'footnote.js', 'purify.js', 'codemirror.css'}:
                self.send_error(404); return
            body = (asset_path('vendor/notes/' + filename)).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/css' if filename.endswith('.css') else 'text/javascript')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers(); self.wfile.write(body); return
        if parsed.path == '/api/business-segments':
            query = parse_qs(parsed.query)
            try:
                self._ai_reply(BusinessSegmentService(services()[0].db).read(
                    query.get('code', [''])[0], start=query.get('start', [None])[0],
                    end=query.get('end', [None])[0]))
            except (ValueError, OSError, StorageError, sqlite3.DatabaseError) as exc:
                self._ai_reply({'error': str(exc)}, 400)
            return
        if parsed.path == '/api/stock-announcements':
            query = parse_qs(parsed.query)
            try:
                code = normalize_code(query.get('code', [''])[0])
                db = services()[0].db
                with db.connection() as conn:
                    instrument = conn.execute('SELECT id FROM instruments WHERE code=?', (code,)).fetchone()
                    rows = conn.execute("""SELECT m.title,m.published_on,m.announcement_id,m.org_id,m.title_category,m.report_type
                        FROM stock_announcement_metadata m
                        WHERE m.instrument_id=? AND m.id IN (
                          SELECT max(id) FROM stock_announcement_metadata WHERE instrument_id=? GROUP BY announcement_id)
                        ORDER BY m.published_on DESC,m.announcement_time DESC,m.id DESC""",
                        (instrument['id'], instrument['id'])).fetchall() if instrument else []
                announcements = []
                for row in rows:
                    category = 'forecast' if is_earnings_notice(row['title']) else row['report_type'] if row['report_type'] in ('annual','interim','q1','q3') else (
                        row['title_category'] if row['title_category'] in ('forecast','dividend') else None)
                    announcements.append({'title': row['title'], 'published_on': row['published_on'],
                        'url': 'https://www.cninfo.com.cn/new/disclosure/detail?' + urlencode({
                            'stockCode': code, 'announcementId': row['announcement_id'], 'orgId': row['org_id']}),
                        'category': category})
                self._ai_reply({'announcements': announcements})
            except (ValueError, OSError, StorageError, sqlite3.DatabaseError) as exc:
                self._ai_reply({'error': str(exc)}, 400)
            return
        if parsed.path=='/api/maintenance/collections/operation':
            try:
                query=parse_qs(parsed.query)
                self._ai_reply(CollectionManagement(Database(DATABASE_PATH),industry_service()).detail(query.get('task_key',[''])[0],query.get('operation_ref',[''])[0]))
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return
        if parsed.path=='/api/maintenance/collections/evidence':
            try:
                batch_id=int(parse_qs(parsed.query).get('batch_id',[''])[0])
                with Database(DATABASE_PATH).connection() as conn:
                    rows=[dict(row) for row in conn.execute('SELECT id,batch_id,source_system,dataset,request_status,parse_status,http_status,error,obtained_at,file_path,raw_sha256,stored_sha256,reused_source_id FROM collection_sources WHERE batch_id=? ORDER BY id DESC LIMIT 200',(batch_id,))]
                self._ai_reply({'batch_id':batch_id,'sources':rows,'limit':200})
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return
        if parsed.path=='/api/maintenance/collections':
            try:
                self._ai_reply(CollectionManagement(Database(DATABASE_PATH),industry_service()).read())
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return
        if parsed.path=='/api/indices/congestion':
            try:
                query=parse_qs(parsed.query)
                self._ai_reply(market_congestion.read(Database(DATABASE_PATH),start=query.get('start',[None])[0],
                    end=query.get('end',[None])[0],index_code=query.get('code',['cn:000001'])[0]))
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return
        if parsed.path in {'/api/indices/catalog','/api/indices/status','/api/indices/chart','/api/indices/price','/api/indices/crowding','/api/indices/industry-crowding','/api/indices/equity-bond-spread'}:
            try:
                with _SERVICE_LOCK:
                    if not hasattr(self.server, 'index_reader'):
                        self.server.index_reader = IndexReadService(Database(DATABASE_PATH))
                    reader = self.server.index_reader
                query = parse_qs(parsed.query)
                if parsed.path.endswith('/catalog'):
                    body = reader.catalog()
                elif parsed.path.endswith('/status'):
                    body = reader.status()
                elif parsed.path.endswith('/price'):
                    body = reader.price_overlay(query.get('code',[None])[0])
                elif parsed.path.endswith('/industry-crowding'):
                    body = reader.industry_crowding(query.get('level',[None])[0],query.get('start',[None])[0],query.get('end',[None])[0])
                elif parsed.path.endswith('/equity-bond-spread'):
                    body = reader.equity_bond_spread(query.get('code',[None])[0])
                elif parsed.path.endswith('/crowding'):
                    body = reader.price_overlay(query.get('code',[None])[0],field='amount_ratio')
                else:
                    body = reader.chart(code=query.get('code',[None])[0],provider=query.get('provider',[None])[0],
                        series=query.get('series',[None])[0],definition=query.get('definition',['v1'])[0],metric=query.get('metric',[None])[0])
                self._ai_reply(body)
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return
        if parsed.path=='/api/indices':
            try:
                query=parse_qs(parsed.query)
                data=read_indices(Database(DATABASE_PATH),code=query.get('code',[None])[0],kind=query.get('kind',[None])[0],
                    level=query.get('level',[None])[0],provider=query.get('provider',[None])[0],series=query.get('series',[None])[0],
                    definition_version=query.get('definition',['v1'])[0],start=query.get('start',[None])[0],end=query.get('end',[None])[0],weekly=query.get('weekly')==['1'])
                self._ai_reply(data)
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return
        if parsed.path=='/api/indices/source':
            try:
                raw,item=read_source(Database(DATABASE_PATH),int(parse_qs(parsed.query).get('id',['0'])[0]))
                # Escape arbitrary source text and keep it inert in the browser.
                self._ai_reply({'metadata':item,'body':raw.decode('utf-8',errors='replace') if item['content_type'] and ('json' in item['content_type'] or 'text' in item['content_type']) else '二进制响应；文件路径及双哈希见元数据'})
            except (ValueError,TypeError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                self._ai_reply({'error':str(exc)},400)
            return
        if parsed.path in {'/indices','/indices.js','/indices.css','/congestion.js'}:
            name='indices.html' if parsed.path=='/indices' else parsed.path.lstrip('/')
            if parsed.path == '/indices':
                body=(asset_path(name)).read_text(encoding='utf-8').replace('__RESEARCH_SIDEBAR__',research_sidebar('indices')).encode('utf-8')
            else:
                body=(asset_path(name)).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type',{'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[name.rsplit('.',1)[1]])
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == '/api/research-reports':
            try:
                self._ai_host()
                self._ai_reply(list_reports(parse_qs(parsed.query).get('code', [''])[0]))
            except (ValueError, OSError, UnicodeError) as exc:
                self._ai_reply({'error': str(exc)}, 400)
            return
        if parsed.path.startswith('/research-reports/'):
            try:
                self._ai_host()
                path = resolve_report(unquote(parsed.path[len('/research-reports/'):]))
                body = path.read_bytes()
            except (ValueError, OSError):
                self.send_error(404, 'Report not found')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path=='/api/maintenance/backups':
            try:
                self._ai_host()
                self._ai_reply({'backups':self.server.restore_manager.list_backups(),'session_token':LOCAL_SESSION_TOKEN,
                    'restore_status':self.server.restore_manager.state})
            except ValueError as exc:self._ai_reply({'error':str(exc)},400)
            return
        if parsed.path.startswith("/api/ai/") or parsed.path == "/api/analysis" or parsed.path.startswith("/api/analysis/") or parsed.path == '/api/business-judgment' or parsed.path.startswith('/api/business-judgment/'):
            self._ai_get(parsed)
            return
        if parsed.path == "/auth/callback":
            self._oauth_callback(parsed)
            return
        status = 200
        if parsed.path == "/plotly.min.js":
            body = get_plotlyjs().encode("utf-8")
            content_type = "text/javascript; charset=utf-8"
        elif parsed.path == "/stock-picker.js":
            body = (asset_path('stock-picker.js')).read_bytes()
            content_type = "text/javascript; charset=utf-8"
        elif parsed.path == "/samples/industry-summary.html":
            body = (asset_path('samples/industry-summary.html')).read_bytes()
            content_type = "text/html; charset=utf-8"
        elif parsed.path in {"/datetime.js", "/research-layout.css", "/ai-assessment.js", "/ai-assessment.css", "/ai-checklist.js", "/ai-checklist.css", '/business-judgment.js',
                             "/financial-statements.js", "/financial-statements.css", "/business-segments.js", "/business-segments.css", "/business-segment-aliases.js", "/business-segment-names.js", "/industry.js", "/industry.css",
                             "/maintenance.js", "/maintenance.css", "/collection-progress.js", "/collection-progress.css", "/investment-notes.js", "/investment-notes.css", "/stock-cleanup.js", "/dcf.js", "/dcf.css",
                             "/dividend-forecast.js", "/dividend-forecast.css", "/shipping-forecast.js", "/shipping-forecast.css"}:
            body = (asset_path(parsed.path.lstrip("/"))).read_bytes()
            content_type = "text/javascript; charset=utf-8" if parsed.path.endswith(".js") else "text/css; charset=utf-8"
        elif parsed.path == "/api/stock-groups":
            body = json.dumps(StockLibrary(services()[0].db).read(), ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif parsed.path == '/api/maintenance/unfollowed':
            body = json.dumps({'stocks':StockLibrary(services()[0].db).unfollowed()}, ensure_ascii=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
        elif parsed.path == '/api/maintenance/stock-cleanup':
            try:
                self._ai_host()
                data = {**self._cleanup_service().candidates(), 'session_token':LOCAL_SESSION_TOKEN}
            except (ValueError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                status,data = 400,{'error':str(exc)}
            body = json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
        elif parsed.path == "/":
            query = parse_qs(parsed.query)
            code = query.get("code", ["601919"])[0]
            if query.get('tab')==['shipping-forecast'] and code=='601919':
                body=(asset_path('shipping-forecast.html')).read_bytes()
            else:
                body = render_page(code, query.get("refresh") == ["1"]).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif parsed.path == '/api/maintenance/storage':
            query=parse_qs(parsed.query)
            try:
                with _SERVICE_LOCK:
                    key=str(DATABASE_PATH)
                    if key not in _MAINTENANCE_SERVICES:
                        _MAINTENANCE_SERVICES[key]=MaintenanceService(Database(DATABASE_PATH))
                    maintenance=_MAINTENANCE_SERVICES[key]
                data=maintenance.read(refresh=query.get('refresh')==['1'])
            except (ValueError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                status,data=503,{'error':str(exc)}
            body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
            content_type='application/json; charset=utf-8'
        elif parsed.path in {'/api/industry','/api/industry/status'}:
            query = parse_qs(parsed.query)
            try:
                data = industry_service().status() if parsed.path.endswith('/status') else industry_service().read(
                    code=query.get('code',[None])[0], industry=query.get('industry',[None])[0],
                    level=int(query.get('level',['3'])[0]), parent=query.get('parent',[None])[0],
                    mode=query.get('mode',['ttm'])[0], period=query.get('period',[None])[0])
            except (ValueError,TypeError) as exc:
                status,data=400,{'error':str(exc)}
            except (OSError,StorageError,sqlite3.DatabaseError) as exc:
                status,data=503,{'error':str(exc)}
            body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
            content_type='application/json; charset=utf-8'
        elif parsed.path == "/api/institution-forecast":
            query = parse_qs(parsed.query)
            try:
                data = fetch_institution_forecast(Database(DATABASE_PATH), query.get('code', [''])[0])
            except (ValueError, TypeError) as exc:
                status, data = 400, {'error': str(exc)}
            except Exception as exc:
                status, data = 503, {'error': '机构预期读取失败：' + str(exc)}
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
        elif parsed.path == "/api/dividend-forecast":
            query = parse_qs(parsed.query)
            try:
                if 'as_of' in query or 'as_of_date' in query:
                    raise ValueError('预测仅支持当前时点，不支持历史回测')
                data = read_dividend_forecast(Database(DATABASE_PATH), query.get('code', ['601919'])[0])
            except (ValueError, TypeError) as exc:
                status, data = 400, {'error': str(exc)}
            except (OSError, StorageError, sqlite3.DatabaseError) as exc:
                status, data = 503, {'error': str(exc)}
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
        elif parsed.path == "/api/dcf":
            query = parse_qs(parsed.query)
            try:
                data = read_dcf(Database(DATABASE_PATH), query.get('code', ['601919'])[0], CACHE)
            except (ValueError, TypeError) as exc:
                status, data = 400, {'error': str(exc)}
            except (OSError, StorageError, sqlite3.DatabaseError) as exc:
                status, data = 503, {'error': str(exc)}
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
        elif parsed.path == "/api/dcf/wacc":
            query = parse_qs(parsed.query)
            try:
                years=int(query.get('years',['5'])[0])
                data=build_auto_wacc(Database(DATABASE_PATH),query.get('code',['601919'])[0],years=years)
            except (ValueError,TypeError) as exc:
                status,data=400,{'error':str(exc)}
            except (OSError,StorageError,sqlite3.DatabaseError) as exc:
                status,data=503,{'error':str(exc)}
            body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
            content_type='application/json; charset=utf-8'
        elif parsed.path == "/api/statements":
            query = parse_qs(parsed.query)
            try:
                data = read_statements(Database(DATABASE_PATH), query.get('code', ['601919'])[0],
                                       period=query.get('period', [None])[0], mode=query.get('mode', ['ytd'])[0],
                                       comparison=query.get('comparison', ['yoy'])[0])
            except ValueError as exc:
                status, data = 400, {'error': str(exc)}
            except (OSError, StorageError, sqlite3.DatabaseError) as exc:
                status, data = 503, {'error': str(exc)}
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            content_type = 'application/json; charset=utf-8'
        elif parsed.path in {"/api/financial", "/api/dividends", "/api/valuation",
                             "/api/shareholders", "/api/financing", "/api/prices"}:
            query = parse_qs(parsed.query)
            try:
                code = query.get("code", ["601919"])[0]
                if Path(DATABASE_PATH).exists() and StockLibrary(services()[0].db).is_unfollowed(normalize_code(code)):
                    raise ValueError('该股票已取消关注，请在后台维护中恢复关注后更新数据')
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
            if status == 200 and parsed.path == '/api/valuation':
                data = compact_valuation(data)
            elif status == 200 and parsed.path == '/api/prices':
                data['valuation'] = compact_valuation(data['valuation'])
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


def _initialize_runtime(db, skip_legacy=False):
    runtime=db.initialize();StockCleanupService(db).recover();db.check();db.daily_backup()
    recover_operations(db)
    recovered=db.recover_interrupted_runs()
    industry_recovered=IndustryService(db).recover_interrupted_runs()
    ai_recovered=ai_service().repository.recover()
    chips,_=services(initialized=True)
    # After restoring, do not silently import newer external cache files into the snapshot.
    if not skip_legacy:
        for section in ('financing','shareholders'):
            for path in sorted((CHIP_CACHE/section).glob('*.json')):
                try:chips.import_legacy(normalize_code(path.stem),section)
                except (ValueError,OSError,StorageError,sqlite3.DatabaseError) as exc:
                    print(f'{path.name} {section} 迁移未完成，原文件保留：{exc}',flush=True)
        for warning in import_p4_legacy(db):print(warning,flush=True)
    print(f"SQLite {runtime['sqlite_version']} / {runtime['journal_mode']} / "
          f"结构版本 {runtime['schema_version']}；恢复中断任务 {recovered} 个",flush=True)
    print(f'AI 中断任务 {ai_recovered} 个；仅手动分析使用 ChatGPT 额度',flush=True)
    print(f'行业中断任务 {industry_recovered} 个；行业仅通过独立入口更新',flush=True)


def _runtime_worker_activity():
    """Check in-memory workers without opening SQLite during exclusive maintenance."""
    with _SERVICE_LOCK:
        for service in _INDUSTRY_SERVICES.values():
            with service._lock:
                if service._status['running']:
                    return True
        return any(
            s.worker and s.worker.is_alive() for s in _AI_SERVICES.values())


def _restore_activity():
    with Database(DATABASE_PATH).connection() as conn:
        index_running=bool(conn.execute("SELECT 1 FROM index_collection_operations WHERE status='running'").fetchone())
    return index_running or _runtime_worker_activity()


def _clear_runtime_services():
    with _SERVICE_LOCK:
        _SERVICES.clear(); _AI_SERVICES.clear(); _INDUSTRY_SERVICES.clear(); _MAINTENANCE_SERVICES.clear()
        _DATA_LOCKS.clear()


def serve(port: int = 8765, open_browser: bool = False):
    return run_server(
        port, open_browser, database_path=DATABASE_PATH, handler=Handler,
        server_factory=ThreadingHTTPServer, initialize=_initialize_runtime,
        activity_check=_restore_activity,
        management_factory=lambda db: CollectionManagement(db, industry_service()),
        clear_state=_clear_runtime_services, open_url=webbrowser.open,
    )
