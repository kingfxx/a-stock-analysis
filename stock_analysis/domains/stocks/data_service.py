"""Stock loading, legacy cache compatibility and shared-price data assembly. Settings are supplied by the application."""
from __future__ import annotations

import json
import shutil
import time
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode
from stock_analysis.domains.stocks.disclosure_dates import is_earnings_notice
import requests
from stock_analysis.domains.financials.core import disclosure_reference_snapshots, disclosure_snapshots, period_end_snapshots
from stock_analysis.infrastructure.network import create_data_session
from stock_analysis.infrastructure.storage import Database, StorageError, instance_lock
from stock_analysis.domains.stocks.price_service import PriceService, month_closes
from stock_analysis.domains.stocks.price_projection import chip_prices, financial_prices, valuation_prices
from stock_analysis.domains.financials.fundamental_service import FundamentalService
from stock_analysis.domains.stocks.announcement_service import AnnouncementService
from stock_analysis.domains.financials.business_segment_service import BusinessSegmentService
from stock_analysis.collection.collection import ProviderBlocked
from stock_analysis.domains.stocks.dividend_service import DividendService
from stock_analysis.domains.valuation.valuation_service import ValuationService
from stock_analysis.domains.stocks.sync_support import instrument_id, sync_state, recently_checked, checked_today, audit_due, backup_before_update, recent_sync_failure
from stock_analysis.domains.stocks.update_service import ChipService
from stock_analysis.infrastructure.storage import SyncKey
from stock_analysis.domains.stocks.stock_collection import begin_stock_batch, complete_stock_batch, fail_stock_batch, recent_failed_stock_batch, routed_stock_requests
from stock_analysis.domains.stocks.stock_library import StockLibrary
from stock_analysis.domains.valuation.valuation_transport import compact_valuation
from stock_analysis.domains.stocks.chips import chip_payload
from stock_analysis.domains.stocks.sources import fetch_cash_flow_reports, fetch_financial_reports, fetch_stock_name
from stock_analysis.domains.stocks.source_parsers import normalize_code, normalize_report_dates
from stock_analysis.domains.valuation.valuation import fetch_dividend_events, fetch_industry_snapshot, fetch_valuation_series, merge_adjusted_prices, merge_dividend_yields, monthly_valuation, monthly_dividend_yields
from stock_analysis.domains.financials import payloads as financial_payloads
from stock_analysis.domains.valuation import payloads as valuation_payloads
from stock_analysis.domains.stocks import data_contracts as contracts


class StockDataService:

    def __init__(self, settings):
        self.settings = settings
        self._SERVICES = {}
        self._DATA_LOCKS = {}
        self._SERVICE_LOCK = Lock()

    def _save_cache(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        if path.exists():
            backup = path.with_suffix(".json.bak")
            backup_temporary = path.with_suffix(".json.bak.tmp")
            shutil.copy2(path, backup_temporary)
            backup_temporary.replace(backup)
        temporary.replace(path)

    def _keep_old(self, old: dict, message: str) -> dict:
        return {**old, "warnings": old.get("warnings", []) + [message]}

    def _missing_old_values(self, old_reports: list[dict], fresh_by_period: dict[str, dict],
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

    def _missing_price_snapshots(self, old: dict, new_prices: dict, reports: list[dict]) -> list[str]:
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

    def _missing_valuation_history(self, old: dict, rows: list[dict], industry: dict, today: str) -> list[str]:
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

    def load_valuation(self, code: str, reports: list[dict], refresh: bool = False) -> dict:
        """Cache compact monthly valuation rows separately from financial snapshots."""
        if Path(self.settings.DATABASE_PATH).exists():
            return self.p4_services()[2].update(code, reports, refresh=refresh)
        path = self.settings.VALUATION_CACHE / f"{code}.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        today = date.today().isoformat()
        if old.get("updated_on") == today and old.get("basis") == contracts.VALUATION_BASIS and not refresh:
            return old
        session = create_data_session()
        warnings = []
        try:
            rows = monthly_valuation(fetch_valuation_series(code, session), reports)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            if old:
                return self._keep_old(old, f"历史估值获取失败，保留原缓存：{exc}")
            rows = old.get("rows", [])
            warnings.append(f"历史估值获取失败，显示已有缓存：{exc}")
        try:
            dividend_yields = self.fetch_dividend_yields(code, session)
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            if old:
                return self._keep_old(old, f"历史股息率获取失败，保留原缓存：{exc}")
            dividend_yields = [{"date": row["dividend_yield_date"], "value": row["dividend_yield"]}
                               for row in old.get("rows", []) if row.get("dividend_yield_date")
                               and row.get("dividend_yield") is not None]
            warnings.append(f"历史股息率获取失败，显示已有缓存：{exc}")
        rows = merge_dividend_yields(rows, dividend_yields)
        try:
            adjusted_prices = self.fetch_monthly_prices(code, session, "qfq")
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            if old:
                return self._keep_old(old, f"前复权月度股价获取失败，保留原缓存：{exc}")
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
                return self._keep_old(old, f"行业估值获取失败，保留原缓存：{exc}")
            industry = old.get("industry", {})
            warnings.append(f"行业估值获取失败，显示已有缓存：{exc}")
        if old:
            missing = self._missing_valuation_history(old, rows, industry, today)
            if missing:
                return self._keep_old(old, f"估值历史覆盖不足（{len(missing)} 项），保留原缓存")
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
        data = {"rows": rows, "industry": industry, "updated_on": today, "basis": contracts.VALUATION_BASIS,
                "warnings": warnings}
        self._save_cache(path, data)
        return data

    def _add_missing_name(self, data: dict, path: Path, session: requests.Session) -> dict:
        if not (data.get("name") or "").strip():
            try:
                name = fetch_stock_name(data["code"], session)
            except (requests.RequestException, ValueError, KeyError):
                name = None
            data = {**data, "name": name}
            self._save_cache(path, data)
        return data

    def _add_sqlite_missing_name(self, db: Database, data: dict, *, refresh=False) -> dict:
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

    def _disclosure_prices(self, code: str, reports: list[dict], session: requests.Session,
                           old: dict | None = None) -> tuple[dict, list[str], bool]:
        dates = sorted({report.get("publish_date") for report in reports if report.get("publish_date")})
        prices, warnings, complete = {}, [], True
        for key, adjust in (("raw", ""), ("qfq", "qfq")):
            reference_key = f"{key}_reference"
            try:
                daily = self.fetch_daily_prices(code, session, adjust, dates[0], dates[-1]) if dates else []
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

    def _migrate_cached(self, code: str, old: dict, path: Path, session: requests.Session) -> dict:
        old = self._add_missing_name(old, path, session)
        if old.get("reports") and old.get("financial_fields_basis") != contracts.FINANCIAL_FIELDS_BASIS:
            try:
                fresh_reports = {report["period"]: report for report in fetch_financial_reports(code, session)}
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                old = self._keep_old(old, f"新增财报字段获取失败：{exc}")
            else:
                missing = self._missing_old_values(old["reports"], fresh_reports, contracts.NEW_REPORT_FIELDS,
                                              require_all_periods=True)
                if missing:
                    old = self._keep_old(old, f"新增财报字段历史覆盖不足（{len(missing)} 期），保留原缓存")
                else:
                    old = {**old, "reports": [
                        {**report, **{field: value for field in contracts.NEW_REPORT_FIELDS
                                     if (value := fresh_reports[report["period"]].get(field)) is not None}}
                        for report in old["reports"]],
                           "financial_fields_basis": contracts.FINANCIAL_FIELDS_BASIS}
                    self._save_cache(path, old)
        cash_warnings = []
        if old.get("cash_flow_basis") != contracts.CASH_FLOW_BASIS:
            try:
                cash_flows = fetch_cash_flow_reports(code, session)
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                cash_warnings.append(f"现金流量表获取失败：{exc}")
                old = {**old, "warnings": old.get("warnings", []) + cash_warnings}
            else:
                missing = self._missing_old_values(old["reports"], cash_flows, contracts.CASH_VALUES)
                if missing:
                    cash_warnings.append(f"现金流量表历史覆盖不足（{len(missing)} 期），保留原缓存")
                    old = {**old, "warnings": old.get("warnings", []) + cash_warnings}
                else:
                    old = {**old, "reports": [
                        {**report, **{field: value for field in contracts.CASH_VALUES
                                     if (value := cash_flows.get(report["period"], {}).get(field)) is not None}}
                        for report in old["reports"]],
                           "cash_flow_basis": contracts.CASH_FLOW_BASIS}
                    self._save_cache(path, old)
        if (old.get("report_date_basis") == contracts.REPORT_DATE_BASIS and old.get("price_basis") == "disclosure"
                and old.get("price_reference_basis") == contracts.PRICE_REFERENCE_BASIS):
            return old
        reports = (old["reports"] if old.get("report_date_basis") == contracts.REPORT_DATE_BASIS
                   else normalize_report_dates(old["reports"]))
        prices, warnings, complete = self._disclosure_prices(code, reports, session, old)
        missing_prices = self._missing_price_snapshots(old, prices, reports) if complete else []
        if missing_prices:
            return self._keep_old(old, f"价格快照历史覆盖不足（{len(missing_prices)} 项），保留原缓存")
        if not complete:
            return self._keep_old(old, "披露日价格获取失败，保留原缓存：" + "；".join(warnings))
        migrated = {**old, "reports": reports, "prices": prices, "price_basis": "disclosure",
                    "report_date_basis": contracts.REPORT_DATE_BASIS,
                    "price_reference_basis": contracts.PRICE_REFERENCE_BASIS if complete else None,
                    "warnings": cash_warnings + warnings}
        if complete:
            self._save_cache(path, migrated)
        return migrated

    def load_stock(self, code: str, refresh: bool = False) -> dict:
        code = normalize_code(code)
        if Path(self.settings.DATABASE_PATH).exists():
            return self.p4_services()[0].update(code, refresh=refresh)
        path = self.settings.CACHE / f"{code}.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        session = create_data_session()
        if old and not refresh:
            return self._migrate_cached(code, old, path, session)
        try:
            reports = fetch_financial_reports(code, session)
        except (requests.RequestException, ValueError, KeyError) as exc:
            if old:
                return self._keep_old(old, f"财报更新失败，保留原缓存：{exc}")
            raise ValueError(f"无法获取 {code} 的季度财报：{exc}") from exc
        if old:
            missing = self._missing_old_values(old.get("reports", []),
                                          {report["period"]: report for report in reports}, contracts.FINANCIAL_VALUES,
                                          require_all_periods=True)
            if missing:
                return self._keep_old(old, f"财报更新未覆盖已有报告期或指标（{len(missing)} 期），保留原缓存")
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
                return self._keep_old(old, f"现金流量表获取失败，保留原缓存：{exc}")
            cash_warnings.append(f"现金流量表获取失败：{exc}")
        else:
            if old:
                missing = self._missing_old_values(old.get("reports", []), cash_flows, contracts.CASH_VALUES)
                if missing:
                    return self._keep_old(old, f"现金流量表历史覆盖不足（{len(missing)} 期），保留原缓存")
            reports = [{**report, **cash_flows.get(report["period"], {})} for report in reports]
            cash_flow_basis = contracts.CASH_FLOW_BASIS
        prices, warnings, complete = self._disclosure_prices(code, reports, session, old)
        if old and not complete:
            return self._keep_old(old, "披露日价格更新失败，保留原缓存：" + "；".join(warnings))
        if old:
            missing_prices = self._missing_price_snapshots(old, prices, reports)
            if missing_prices:
                return self._keep_old(old, f"价格快照历史覆盖不足（{len(missing_prices)} 项），保留原缓存")
        warnings = cash_warnings + warnings
        try:
            name = fetch_stock_name(code, session)
        except (requests.RequestException, ValueError, KeyError) as exc:
            name = old.get("name") if old else None
            if not name:
                warnings.append(f"股票名称获取失败：{exc}")
        data = {"code": code, "updated_at": datetime.now(timezone.utc).isoformat(),
                "name": name, "reports": reports, "prices": prices,
                "price_basis": "disclosure", "report_date_basis": contracts.REPORT_DATE_BASIS,
                "cash_flow_basis": cash_flow_basis,
                "financial_fields_basis": contracts.FINANCIAL_FIELDS_BASIS,
                "price_reference_basis": contracts.PRICE_REFERENCE_BASIS if complete else None,
                "warnings": warnings}
        if old and "dividend_events" in old:
            data["dividend_events"] = old["dividend_events"]
            data["dividend_basis"] = old.get("dividend_basis")
        self._save_cache(path, data)
        return data

    def load_dividends(self, data: dict, refresh: bool = False) -> tuple[list[dict] | None, list[str]]:
        """Cache implemented dividend events with stock fundamentals; protect older history."""
        if Path(self.settings.DATABASE_PATH).exists():
            updated = self.p4_services()[1].update(data["code"], refresh=refresh)
            return updated["events"], updated["warnings"]
        old_events = data.get("dividend_events")
        if data.get("dividend_basis") == contracts.DIVIDEND_BASIS and old_events and not refresh:
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
        path = self.settings.CACHE / f'{data["code"]}.json'
        with self._DATA_LOCKS.setdefault("financial:" + data["code"], Lock()):
            latest = self._read_cache(path) or data
            data.update(latest, dividend_events=events, dividend_basis=contracts.DIVIDEND_BASIS)
            self._save_cache(path, data)
        return events, []

    def cached_stocks(self) -> list[dict]:
        if Path(self.settings.DATABASE_PATH).exists():
            db = self.services()[0].db
            with db.connection() as conn:
                return [{"code": row["code"], "name": row["name"]} for row in conn.execute(
                    "SELECT code,name FROM instruments WHERE id NOT IN (SELECT instrument_id FROM stock_unfollowed) ORDER BY code")]
        stocks = []
        for path in sorted(self.settings.CACHE.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
            stocks.append({"code": data["code"], "name": data.get("name")})
        return stocks

    def _read_cache(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _needs_financial(self, data: dict) -> bool:
        return (not data or not (data.get("name") or "").strip()
                or data.get("financial_fields_basis") != contracts.FINANCIAL_FIELDS_BASIS
                or data.get("cash_flow_basis") != contracts.CASH_FLOW_BASIS
                or data.get("report_date_basis") != contracts.REPORT_DATE_BASIS
                or data.get("price_basis") != "disclosure"
                or data.get("price_reference_basis") != contracts.PRICE_REFERENCE_BASIS)



    def _financial_with_announcement_state(self, data, db):
        if data and data.get("code"):
            sync = AnnouncementService(db).read(data["code"]).get("sync", {})
            data = {**data, "announcement_sync": sync}
            data['business_segment_sync'] = BusinessSegmentService(db).read(data['code']).get('sync', {})
        return data

    def services(self, *, initialized=False):
        key = (str(Path(self.settings.DATABASE_PATH).resolve()), str(self.settings.CHIP_CACHE.resolve()))
        with self._SERVICE_LOCK:
            if key not in self._SERVICES:
                db = Database(self.settings.DATABASE_PATH)
                if not initialized:
                    with instance_lock(db.path):
                        db.initialize()
                self._SERVICES[key] = (ChipService(db, self.settings.CHIP_CACHE), PriceService(db))
            return self._SERVICES[key]

    def p4_services(self):
        db = self.services()[0].db
        return (FundamentalService(db, self.settings.CACHE), DividendService(db, self.settings.CACHE),
                ValuationService(db, self.settings.VALUATION_CACHE, fetch_industry=ValuationService._fetch_industry))

    def import_p4_legacy(self, db):
        """One-time, dataset-qualified imports; damaged files never block other stocks."""
        importers = ((FundamentalService(db, self.settings.CACHE), self.settings.CACHE),
                     (DividendService(db, self.settings.CACHE), self.settings.CACHE),
                     (ValuationService(db, self.settings.VALUATION_CACHE), self.settings.VALUATION_CACHE))
        warnings = []
        for importer, root in importers:
            for path in sorted(root.glob("*.json")):
                try:
                    importer.import_legacy(normalize_code(path.stem))
                except (ValueError, KeyError, TypeError, OSError, StorageError, sqlite3.DatabaseError) as exc:
                    warnings.append(f"{path.name} {type(importer).__name__} 导入未完成，原文件保留：{exc}")
        return warnings

    def p4_loading(self, code):
        db = self.services()[0].db
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

    def fetch_daily_prices(self, code, session, adjust, earliest_date, latest_date):
        """All production consumers share the same stored daily source history."""
        prices = self.services()[1]
        result = prices.ensure(code, adjust or "raw")
        if result["warnings"]:
            raise ValueError("；".join(result["warnings"]))
        start = (date.fromisoformat(earliest_date) - timedelta(days=180)).isoformat()
        end = min(date.today(), date.fromisoformat(latest_date) + timedelta(days=180)).isoformat()
        return prices.read(code, adjust or "raw", start=start, end=end)

    def fetch_monthly_prices(self, code, session, adjust="qfq"):
        prices = self.services()[1]
        result = prices.ensure(code, adjust or "raw")
        if result["warnings"]:
            raise ValueError("；".join(result["warnings"]))
        return month_closes(prices.read(code, adjust or "raw"))

    def fetch_dividend_yields(self, code, session):
        if Path(self.settings.DATABASE_PATH).exists():
            _, dividends, _ = self.p4_services()
            raw = self.services()[1].read(code, "raw")
            return monthly_dividend_yields(raw, dividends.read(code))
        events = fetch_dividend_events(code, session)
        end = date.today()
        start = (end - timedelta(days=3660)).isoformat()
        return monthly_dividend_yields(self.fetch_daily_prices(code, session, "", start, end.isoformat()), events)

    def shared_projection(self, code, version=None, *, lease=False):
        chips, prices = self.services()
        version = prices.current_version(code) if version is None else version
        raw = prices.read(code, "raw")
        qfq = prices.read(code, "qfq", version=version or 0, lease=lease)
        return version, raw, qfq

    def load_chips(self, code: str, section: str, refresh: bool = False, *, full=False, price_version=None) -> dict:
        chips, prices = self.services()
        data = chips.update(code, section, refresh=refresh, full=full)
        version, raw, qfq = self.shared_projection(code, price_version, lease=True)
        return {**chip_prices(data, section, raw, qfq, version),
                "price_validated_at": prices.version_info(code, version).get("validated_at")}

    def price_bundle(self, code, *, refresh=False, full=False, version=None):
        code = normalize_code(code)
        chips, prices = self.services()
        warnings = []
        if version is None:
            for adjustment in ("raw", "qfq"):
                try:
                    warnings += prices.ensure(code, adjustment, refresh=refresh, full=full)["warnings"]
                except ValueError as exc:
                    warnings.append(str(exc))
        version, raw, qfq = self.shared_projection(code, version, lease=True)
        fundamentals, dividends, valuations = self.p4_services()
        financial = self._financial_with_announcement_state(fundamentals.read(code), fundamentals.db)
        events = dividends.read(code)
        financial["dividend_events"] = events
        financial["dividend_basis"] = contracts.DIVIDEND_BASIS if events else None
        valuation = valuations.read(code, financial.get("reports", []))
        if not version:
            warnings.append("共享前复权尚未就绪，前复权价格留空；披露日估算市值独立使用已有未复权价格。")
        price_meta = prices.version_info(code, version)
        holders = chip_prices(chips.cached(code, "shareholders"), "shareholders", raw, qfq, version)
        financing = chip_prices(chips.cached(code, "financing"), "financing", raw, qfq, version)
        for data in (holders, financing):
            data["price_validated_at"] = price_meta.get("validated_at")
        return dict(price_version=version, price_context=price_meta, warnings=warnings,
                    financial=financial_payloads._financial_payload(financial_prices(financial, raw, qfq)),
                    valuation=valuation_payloads._p4_valuation_payload(valuation, raw=raw, qfq=qfq, events=events),
                    shareholders=holders, financing=financing)

    def load_chart_data(self, code: str, section: str, refresh: bool = False, *, price_version=None, full=False) -> dict:
        """Network work runs in separate requests; serialize writes to each stock cache."""
        code = normalize_code(code)
        if section not in {"financial", "dividends", "valuation", "shareholders", "financing"}:
            raise ValueError("未知数据类型")
        lock_key = section + ":" + code
        with self._DATA_LOCKS.setdefault(lock_key, Lock()):
            if section in {"shareholders", "financing"}:
                return self.load_chips(code, section, refresh, price_version=price_version, full=full)
            if Path(self.settings.DATABASE_PATH).exists():
                fundamentals, dividends, valuations = self.p4_services()
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
                        data = self._financial_with_announcement_state(data, fundamentals.db)
                    data = self._add_sqlite_missing_name(fundamentals.db, data, refresh=refresh or full)
                    data['business_segment_sync'] = segments.get('sync', {})
                    data["dividend_events"] = dividends.read(code)
                    data["dividend_basis"] = contracts.DIVIDEND_BASIS if data["dividend_events"] else None
                    if price_version is not None or self.services()[1].read(code, "raw"):
                        _, raw, qfq = self.shared_projection(code, price_version, lease=True)
                        data = financial_prices(data, raw, qfq)
                    return {**financial_payloads._financial_payload(data), "cached_stocks": self.cached_stocks(),
                            "price_version": price_version}
                if section == "dividends":
                    data = fundamentals.read(code)
                    updated = dividends.update(code, refresh=refresh, full=full)
                    data["dividend_events"] = updated["events"]
                    data["dividend_basis"] = contracts.DIVIDEND_BASIS if updated["events"] else None
                    data["warnings"] += updated["warnings"]
                    if price_version is not None or self.services()[1].read(code, "raw"):
                        _, raw, qfq = self.shared_projection(code, price_version, lease=True)
                        data = financial_prices(data, raw, qfq)
                    return {**financial_payloads._financial_payload(data), "cached_stocks": self.cached_stocks(),
                            "price_version": price_version}
                reports = fundamentals.read(code).get("reports", [])
                result = valuations.update(code, reports, refresh=refresh, full=full)
                _, raw, qfq = self.shared_projection(code, price_version, lease=True)
                result["warnings"] += []
                return {**valuation_payloads._p4_valuation_payload(result, raw=raw, qfq=qfq, events=dividends.read(code)),
                        "price_version": price_version}
            if section == "financial":
                data = self.load_stock(code, refresh)
            else:
                data = self._read_cache(self.settings.CACHE / f"{code}.json")
                if not data:
                    raise ValueError("财报尚未加载，请稍后重试")
                if section == "valuation":
                    reports = data.get("reports", [])
                    if data.get("report_date_basis") != contracts.REPORT_DATE_BASIS:
                        reports = normalize_report_dates(reports)
                    valuation = self.load_valuation(code, reports, refresh)
                    if price_version:
                        _, _, qfq = self.shared_projection(code, price_version, lease=True)
                        valuation = valuation_prices(valuation, qfq)
                    return {**valuation_payloads._valuation_payload(valuation), "price_version": price_version}
                events, warnings = self.load_dividends(data, refresh)
                data = self._read_cache(self.settings.CACHE / f"{code}.json") or data
                data = {**data, "dividend_events": events,
                        "warnings": data.get("warnings", []) + warnings}
            if price_version:
                _, raw, qfq = self.shared_projection(code, price_version, lease=True)
                data = financial_prices(data, raw, qfq)
            return {**financial_payloads._financial_payload(data), "cached_stocks": self.cached_stocks(), "price_version": price_version}

    def _consumer_cache(self, path, warnings):
        """One damaged legacy consumer must not hide independent SQLite facts."""
        try:
            data = self._read_cache(path)
            if not isinstance(data, dict):
                raise ValueError("缓存不是对象")
            return data
        except (ValueError, OSError) as exc:
            warnings.append(f"{path.parent.name} 缓存不可用，原文件保留：{exc}")
            return {}

    def page_payload(self, code: str, refresh: bool) -> tuple[str, dict, str]:
        """Return cached charts immediately, without making any external requests."""
        payload = {"code": code, "views": {}, "warnings": [], "valuation": {"views": {}},
                   "loading": {"financial": True, "dividends": True, "valuation": True},
                   "refresh_requested": refresh, "price_version": None, "price_needs_update": True}
        cache_warnings = []
        try:
            code = normalize_code(code)
            if Path(self.settings.DATABASE_PATH).exists():
                fundamentals, dividends, valuations = self.p4_services()
                data = self._financial_with_announcement_state(fundamentals.read(code), fundamentals.db)
                data["dividend_events"] = dividends.read(code)
                data["dividend_basis"] = contracts.DIVIDEND_BASIS if data["dividend_events"] else None
                valuation = valuations.read(code, data.get("reports", []))
            else:
                data = self._consumer_cache(self.settings.CACHE / f"{code}.json", cache_warnings)
                valuation = self._consumer_cache(self.settings.VALUATION_CACHE / f"{code}.json", cache_warnings)
            payload.update(financial_payloads._financial_payload(data))
            payload["valuation"] = (valuation_payloads._p4_valuation_payload(valuation, events=data.get("dividend_events"))
                                    if Path(self.settings.DATABASE_PATH).exists() else valuation_payloads._valuation_payload(valuation))
            payload["loading"] = {"financial": self._needs_financial(data),
                                  "dividends": contracts.needs_dividends(data),
                                  "valuation": not valuation
                                  or valuation.get("updated_on") != date.today().isoformat()
                                  or valuation.get("basis") not in (contracts.VALUATION_BASIS, "baidu_dense_observations_v1")}
            if Path(self.settings.DATABASE_PATH).exists():
                payload["loading"] = self.p4_loading(code)
            payload["price_version"] = None
            payload["price_needs_update"] = True
            raw, qfq, sql_chips = [], [], None
            if Path(self.settings.DATABASE_PATH).exists():
                sql_chips, price_service = self.services()
                version, raw, qfq = self.shared_projection(code)
                payload["price_version"] = version
                # Read-only inspection: a page visit never creates stocks or leases.
                identity = instrument_id(sql_chips.db, code)
                if identity:
                    payload["price_needs_update"] = any(not checked_today(state := sync_state(sql_chips.db,
                        SyncKey(identity, dataset, "tencent", adjustment))) or audit_due(state, dataset) for dataset, adjustment in
                        (("prices_raw", "raw"), ("prices_adjusted", "qfq")))
                normalized = ({**data, "reports": (data.get("reports", [])
                                  if data.get("report_date_basis") == contracts.REPORT_DATE_BASIS
                                  else normalize_report_dates(data.get("reports", []))),
                              "report_date_basis": contracts.REPORT_DATE_BASIS} if data else {})
                if version or raw:
                    payload.update(financial_payloads._financial_payload(financial_prices(normalized, raw, qfq)))
                    payload["valuation"] = valuation_payloads._p4_valuation_payload(valuation, raw=raw, qfq=qfq,
                                                                events=data.get("dividend_events"))
            for section in ("shareholders", "financing"):
                cached = sql_chips.cached(code, section) if sql_chips else {}
                if cached.get("stored_count") or cached.get("empty"):
                    payload[section] = chip_prices(cached, section, raw, qfq, payload["price_version"])
                    payload["loading"][section] = cached.get("needs_update", True)
                else:
                    try:
                        chips = self._read_cache(self.settings.CHIP_CACHE / section / f"{code}.json")
                        fallback = chip_payload(chips, section)
                    except (ValueError, KeyError, TypeError, OSError) as exc:
                        chips, fallback = {}, {"rows": [], "warnings": [f"旧缓存不可用：{exc}"]}
                    payload[section] = chip_prices(fallback, section, raw, qfq, payload["price_version"])
                    payload["loading"][section] = bool(chips) or not cached.get("empty")
            error = "；".join(cache_warnings)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            error = str(exc)
        payload["cached_stocks"] = self.cached_stocks()
        payload["stock_library"] = StockLibrary(self.services()[0].db).read() if Path(self.settings.DATABASE_PATH).exists() else {}
        payload["unfollowed"] = (StockLibrary(self.services()[0].db).is_unfollowed(code) if Path(self.settings.DATABASE_PATH).exists() else False)
        payload['valuation'] = compact_valuation(payload['valuation'])
        return code, payload, error

    def read_announcements(self, code):
        code = normalize_code(code)
        db = self.services()[0].db
        with db.connection() as conn:
            instrument = conn.execute('SELECT id FROM instruments WHERE code=?', (code,)).fetchone()
            rows = conn.execute('SELECT m.title,m.published_on,m.announcement_id,m.org_id,m.title_category,m.report_type\n                        FROM stock_announcement_metadata m\n                        WHERE m.instrument_id=? AND m.id IN (\n                          SELECT max(id) FROM stock_announcement_metadata WHERE instrument_id=? GROUP BY announcement_id)\n                        ORDER BY m.published_on DESC,m.announcement_time DESC,m.id DESC', (instrument['id'], instrument['id'])).fetchall() if instrument else []
        announcements = []
        for row in rows:
            category = 'forecast' if is_earnings_notice(row['title']) else row['report_type'] if row['report_type'] in ('annual', 'interim', 'q1', 'q3') else row['title_category'] if row['title_category'] in ('forecast', 'dividend') else None
            announcements.append({'title': row['title'], 'published_on': row['published_on'], 'url': 'https://www.cninfo.com.cn/new/disclosure/detail?' + urlencode({'stockCode': code, 'announcementId': row['announcement_id'], 'orgId': row['org_id']}), 'category': category})
        return {'announcements': announcements}
