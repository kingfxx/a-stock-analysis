"""Incremental Sina report facts, with derived reports rebuilt from saved history."""

from __future__ import annotations

import json
import hashlib
from datetime import date
from pathlib import Path

from stock_analysis.infrastructure.network import create_data_session
from stock_analysis.domains.stocks.sources import fetch_financial_report_page
from stock_analysis.domains.stocks.source_parsers import PROFIT_KEYS, normalize_code, normalize_report_dates, parse_cash_flow_reports, parse_financial_reports, parse_adjusted_profit_reports
from stock_analysis.infrastructure.storage import Database, SyncKey, SyncResult, utc_now
from stock_analysis.domains.stocks.stock_collection import begin_stock_batch, routed_stock_requests
from stock_analysis.domains.stocks.disclosure_dates import apply_disclosure_dates
from stock_analysis.domains.stocks.sync_support import audit_due, backup_before_update, data_lock, mark_failed, next_audit, recently_checked, recent_sync_failure, sync_state


SOURCE = "sina:CompanyFinanceService.getFinanceReport2022"
STATEMENT_TYPES = ("lrb", "fzb", "llb")
REPORT_TYPES = STATEMENT_TYPES + ("gjzb",)


def _income_fields(raw):
    populated = {item.get("item_title") for item in raw.get("data", [])
                 if item.get("item_value") not in (None, "", "--")}
    return bool({"营业收入", "营业总收入"} & populated), bool(set(PROFIT_KEYS) & populated)


class FundamentalService:
    def __init__(self, db: Database, legacy_root: Path, *, fetch_page=None):
        self.db = db
        self.legacy_root = Path(legacy_root)
        self.fetch_page = fetch_page or self._fetch_page

    @staticmethod
    def _fetch_page(code, kind, num, page):
        with create_data_session() as session:
            return fetch_financial_report_page(code, session, kind, num=num, page=page)

    @staticmethod
    def _iso_period(value):
        token = str(value)
        day = date.fromisoformat(token if "-" in token else f"{token[:4]}-{token[4:6]}-{token[6:8]}")
        return day.isoformat()

    @staticmethod
    def _date(value):
        if not value:
            return None
        return FundamentalService._iso_period(str(value)[:10])

    def _collect(self, code, kind, *, full, saved):
        size = 200 if full else 2
        expected_overlap = {row["period"] for row in (saved if full else saved[-2:])}
        by_period, first_page, page, total = {}, None, 1, None
        while True:
            response = self.fetch_page(code, kind, size, page)
            records = response["records"]
            if not isinstance(records, dict) or not records and page == 1 and response["total"]:
                raise ValueError(f"新浪{kind}返回空页或无效记录")
            count = int(response["total"])
            if count < 0 or total is not None and total != count:
                raise ValueError(f"新浪{kind}分页总数变化")
            total = count
            if page == 1:
                first_page = records
            for key, raw in records.items():
                period = self._iso_period(key)
                if period in by_period and by_period[period] != raw:
                    raise ValueError(f"新浪{kind}分页重叠报告不一致：{period}")
                by_period[period] = raw
            if len(by_period) >= total if full else expected_overlap <= by_period.keys():
                break
            if not records or len(records) < size or page * size >= total:
                raise ValueError(f"新浪{kind}分页缺失旧报告期或总数不符")
            page += 1
        if page > 1 and self.fetch_page(code, kind, size, 1)["records"] != first_page:
            raise ValueError(f"新浪{kind}分页期间首页变化")
        if expected_overlap - by_period.keys():
            raise ValueError(f"新浪{kind}漏掉已知报告期")
        if kind == "lrb":
            recent_periods = set(sorted(by_period, reverse=True)[:8])
            for period, raw in by_period.items():
                if period in recent_periods and not all(_income_fields(raw)):
                    raise ValueError(f"新浪利润表 {period} 缺少营收或归母利润")
        previous_by_period = {row["period"]: json.loads(row["raw_json"]) for row in saved}
        for period, raw in by_period.items():
            previous = previous_by_period.get(period)
            if not previous:
                continue
            old_fields = {(item.get("item_title"), item.get("item_field"))
                          for item in previous.get("data", []) if item.get("item_value") not in (None, "", "--")}
            fresh_fields = {(item.get("item_title"), item.get("item_field"))
                            for item in raw.get("data", []) if item.get("item_value") not in (None, "", "--")}
            if old_fields - fresh_fields:
                raise ValueError(f"新浪{kind} {period} 丢失已有字段，保留旧事实")
        return [{"period": period, "publish_date": self._date(raw.get("publish_date")),
                 "update_time": str(raw.get("update_time")) if raw.get("update_time") is not None else None,
                 "raw_json": raw} for period, raw in sorted(by_period.items())]

    def read(self, code):
        code = normalize_code(code)
        with self.db.connection() as conn:
            instrument = conn.execute("SELECT id,name FROM instruments WHERE code=?", (code,)).fetchone()
        if not instrument:
            return {"code": code, "name": None, "reports": [], "warnings": []}
        rows = {kind: self.db.financial_reports(instrument["id"], SOURCE, kind)
                for kind in REPORT_TYPES}
        def payload(kind):
            return {"result": {"data": {"report_list": {
                row["period"].replace("-", ""): json.loads(row["raw_json"])
                for row in rows[kind]}}}}
        if rows["lrb"]:
            reports = parse_financial_reports(payload("lrb"), payload("fzb"))
            adjusted_profit = parse_adjusted_profit_reports(rows["gjzb"])
            for report in reports:
                if adjusted_profit.get(report["period"]) is not None:
                    report["adjusted_profit_ytd"] = adjusted_profit[report["period"]]
            if rows["llb"]:
                cash = parse_cash_flow_reports(payload("llb"))
                reports = [{**report, **cash.get(report["period"], {})} for report in reports]
            reports = normalize_report_dates(reports)
        else:
            reports = []
        legacy = self.db.financial_reports(instrument["id"], "legacy", "merged")
        by_period = {report["period"]: report for report in reports}
        for saved in legacy:
            original = json.loads(saved["raw_json"])
            period = saved["period"]
            if period in by_period:
                by_period[period] = {**original, **{field: value for field, value in by_period[period].items()
                                                   if value is not None}}
            else:
                by_period[period] = original
        reports = normalize_report_dates([by_period[period] for period in sorted(by_period)])
        with self.db.connection() as conn:
            overrides = conn.execute("SELECT period,field_name,value_json FROM report_overrides "
                                     "WHERE instrument_id=?", (instrument["id"],)).fetchall()
            disclosure_rows = conn.execute("SELECT * FROM report_disclosure_dates WHERE instrument_id=? AND is_current=1",
                                           (instrument["id"],)).fetchall()
            updated_at = conn.execute(
                "SELECT MAX(succeeded_at) FROM sync_state WHERE instrument_id=? AND data_status='data' "
                "AND ((source=? AND dataset IN ('financial:lrb','financial:fzb','financial:llb')) "
                "OR (source='legacy' AND dataset='financial:merged'))",
                (instrument["id"], SOURCE)).fetchone()[0]
        disclosure = {'dates': {row['period']: dict(id=row['id'], first_published_on=row['first_published_on'],
            latest_revision_on=row['latest_revision_on'], status=row['status'], reason=row['reason'],
            rule_version=row['rule_version']) for row in disclosure_rows}}
        reports = apply_disclosure_dates(reports, disclosure)
        by_period = {report["period"]: report for report in reports}
        for override in overrides:
            if override["period"] in by_period:
                by_period[override["period"]][override["field_name"]] = json.loads(override["value_json"])
                if override["field_name"] == "publish_date":
                    by_period[override["period"]]["_publish_date_override_applied"] = True
                    by_period[override["period"]]["publish_date_source"] = "manual"
                    by_period[override["period"]]["publish_date_status"] = "manual_override"
                    by_period[override["period"]]["publish_date_reason"] = "人工覆盖优先"
                    by_period[override["period"]]["_publish_date_finalized"] = True
        return {"code": code, "name": instrument["name"], "reports": reports,
                "updated_at": updated_at if reports else None,
                "prices": {}, "price_basis": "disclosure", "report_date_basis": "sina_same_period_shift_v1",
                "cash_flow_basis": "sina_cash_flow_ytd_v1" if rows["llb"] else None,
                "financial_fields_basis": "sina_margin_debt_roic_inputs_v2",
                "price_reference_basis": "long_trading_gap_v1", "warnings": []}

    def import_legacy(self, code):
        """Import old combined reports and user hooks without inventing Sina raw records."""
        code = normalize_code(code)
        path = self.legacy_root / f"{code}.json"
        if not path.exists():
            return False
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        document = json.loads(content.decode("utf-8"))
        if document.get("code") != code or not isinstance(document.get("reports"), list):
            raise ValueError("Legacy financial cache has a mismatched code or report list")
        identity = self.db.ensure_instrument(code, document.get("name"))
        reports = document["reports"]
        periods = []
        for report in reports:
            period = self._iso_period(report["period"])
            if period in periods:
                raise ValueError("Legacy financial cache has duplicate report periods")
            periods.append(period)
        for dataset in ("financial:merged", "report_overrides"):
            with self.db.connection() as conn:
                existing = conn.execute("SELECT content_hash FROM legacy_imports WHERE path=? AND dataset=?",
                                        (str(path.resolve()), dataset)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise ValueError(f"Legacy cache changed after {dataset} import; keep both versions for review")
                continue
            key = SyncKey(identity, dataset, "legacy")
            run = self.db.start_sync(key, parser_version="legacy_json_v1",
                                     methodology_version="legacy_snapshot_v1", trigger_reason="legacy_import")
            try:
                if dataset == "financial:merged":
                    rows = [{"period": self._iso_period(report["period"]),
                             "publish_date": report.get("source_publish_date", report.get("publish_date")),
                             "update_time": str(report.get("source_update_time")) if report.get("source_update_time") is not None else None,
                             "raw_json": report} for report in reports]
                    def write(conn):
                        self.db.upsert_financial_reports(conn, key, run, rows)
                        self.db.record_legacy_import(conn, key, run, str(path.resolve()), digest, len(rows))
                    count = len(rows)
                else:
                    overrides = [(self._iso_period(report["period"]), field, report[field])
                                 for report in reports for field in
                                 ("excess_cash", "non_operating_adjustments_ytd") if field in report]
                    def write(conn):
                        for period, field, value in overrides:
                            conn.execute("INSERT INTO report_overrides(instrument_id,period,field_name,value_json,"
                                         "origin,source_path,updated_at,run_id) VALUES (?,?,?,?,?,?,?,?) "
                                         "ON CONFLICT(instrument_id,period,field_name) DO NOTHING",
                                         (identity, period, field, json.dumps(value, ensure_ascii=False),
                                          "legacy", str(path.resolve()), utc_now(), run))
                        self.db.record_legacy_import(conn, key, run, str(path.resolve()), digest, len(overrides))
                    count = len(overrides)
                result = SyncResult(count, min(periods) if count and periods else None,
                                    max(periods) if count and periods else None,
                                    max(periods) if count and periods else None, no_data=count == 0)
                self.db.complete_sync(run, result, write)
            except Exception as exc:
                mark_failed(self.db, run, exc)
                raise
        return True

    def update(self, code, *, refresh=False, full=False):
        code = normalize_code(code)
        backup_before_update(self.db)
        with data_lock(self.db, "financial", code):
            identity = self.db.ensure_instrument(code)
            warnings = []
            for kind in REPORT_TYPES:
                key = SyncKey(identity, f"financial:{kind}", SOURCE)
                state = sync_state(self.db, key)
                recent_failure = recent_sync_failure(self.db, key) if not (refresh or full) else None
                if recent_failure:
                    warnings.append(f"{kind} 最近自动更新失败，暂缓重试：{recent_failure['error']}")
                    continue
                saved = self.db.financial_reports(identity, SOURCE, kind)
                if saved and not (refresh or full or audit_due(state, "financial")
                                  or not recently_checked(state, seconds=86400)):
                    continue
                full_kind = full or not saved or audit_due(state, "financial")
                run = self.db.start_sync(key, parser_version="sina_reports_v1",
                                         methodology_version="raw_facts_v1",
                                         trigger_reason="full" if full_kind else "refresh")
                batch = begin_stock_batch(self.db, run, "sina", f"financial.{kind}", code,
                                          trigger="manual_full" if full else "manual_refresh" if refresh else "automatic",
                                          scope={"source_interface": f"getFinanceReport2022:{kind}",
                                                 "mode": "full" if full_kind else "incremental"},
                                          parser_version="sina_reports_v1", definition_version="raw_facts_v1")
                try:
                    with routed_stock_requests(self.db, batch, "sina", f"financial.{kind}"):
                        candidates = self._collect(code, kind, full=full_kind, saved=saved)
                    all_periods = sorted({row["period"] for row in saved} |
                                         {row["period"] for row in candidates})
                    if not all_periods:
                        raise ValueError(f"新浪{kind}无财报记录")
                    result = SyncResult(len(candidates), all_periods[0], all_periods[-1],
                                        all_periods[-1],
                                        next_full_audit_at=next_audit("financial") if full_kind else state.get("next_full_audit_at"))
                    self.db.complete_sync(run, result,
                        lambda conn: self.db.upsert_financial_reports(conn, key, run, candidates),
                        collection_batch_id=batch,
                        batch_result={"row_count": len(candidates), "sync_run_id": run})
                    if kind == "lrb" and full_kind:
                        sparse = [row["period"] for row in candidates
                                  if not all(_income_fields(row["raw_json"]))]
                        if sparse:
                            warnings.append("旧期利润表来源缺少营收或归母利润，原始报告已保存，缺失指标留空："
                                            + "、".join(sparse))
                except Exception as exc:
                    mark_failed(self.db, run, exc, collection_batch_id=batch)
                    warnings.append(f"{kind} 财报更新失败，保留已存事实：{exc}")
            data = self.read(code)
            data["warnings"] = warnings
            return data
