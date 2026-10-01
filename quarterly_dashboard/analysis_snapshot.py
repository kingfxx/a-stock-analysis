"""Local-only, consistent evidence snapshots. Never fetch sources here."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from .core import build_period_rows, view_rows, DEBT_COMPONENTS
from .fundamental_service import FundamentalService
from .dividend_service import DividendService
from .valuation_service import ValuationService
from .price_service import PriceService
from .valuation import valuation_summary_observations, merge_dividend_yields, monthly_dividend_yields
from .sources import normalize_code
from .storage import utc_now

INPUT_VERSION = "stock_assessment_input_v1"
CALCULATION_VERSION = "stock_assessment_calc_v1"
PROFILE = {"style": "value", "horizon": "1_to_3_years"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class SnapshotError(ValueError):
    pass


def capture(db, code, *, as_of=None):
    code = normalize_code(code)
    today = as_of or date.today().isoformat()
    with db.shared_reader() as reader:
        conn = reader.conn
        instrument = conn.execute("SELECT * FROM instruments WHERE code=?", (code,)).fetchone()
        if not instrument:
            raise SnapshotError("没有本地财报，请先刷新股票数据")
        identity = instrument["id"]
        # Existing repositories now share this read transaction, including overrides.
        financial = FundamentalService(reader, Path(".")).read(code)
        dividends = DividendService(reader, Path(".")).read(code)
        valuation = ValuationService(reader, Path(".")).read(code, financial["reports"])
        price_service = PriceService(reader)
        version = price_service.current_version(code)
        raw = price_service.read(code, "raw", lease=False)
        qfq = price_service.read(code, "qfq", version=version or 0, lease=False)
        def rows(table, order):
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM {table} WHERE instrument_id=? ORDER BY {order}", (identity,))]
        holders = rows("shareholder_observations", "stat_date,source,source_record_key")[-8:]
        financing = rows("financing_daily", "trade_date,source")
        if financing:
            financing = [r for r in financing if r["source"] == financing[-1]["source"]][-121:]
        states = rows("sync_state", "dataset,source,adjustment")
        active_updates = conn.execute("SELECT count(*) FROM sync_runs WHERE instrument_id=? AND status='running'",
                                      (identity,)).fetchone()[0]
        recent_failures = [dict(r) for r in conn.execute(
            "SELECT r.dataset,r.source,r.status,r.finished_at FROM sync_runs r WHERE r.instrument_id=? "
            "AND r.status IN ('failed','superseded') AND r.id=(SELECT max(last.id) FROM sync_runs last "
            "WHERE last.instrument_id=r.instrument_id AND last.dataset=r.dataset AND last.source=r.source AND last.adjustment=r.adjustment)",
            (identity,))]
        industry_rows = rows("industry_snapshots", "snapshot_at,id")[-1:]
        manifest = {"price_version": version, "financial": rows("financial_reports", "period,source,report_type"),
                    "overrides": rows("report_overrides", "period,field_name"),
                    "dividends": rows("dividend_events", "id"),
                    "valuation": rows("valuation_observations", "observed_on,source,metric"),
                    "legacy_valuation": rows("legacy_valuation_snapshots", "observed_month"),
                    "industry": industry_rows, "shareholders": holders, "financing": financing,
                    "raw_prices": raw, "qfq_prices": qfq, "sync_state": states}
    if not financial["reports"]:
        raise SnapshotError("没有可读财报，请先刷新数据")
    if not raw and not valuation.get("rows") and not valuation.get("observation_rows"):
        raise SnapshotError("没有可用价格或估值事实，请先刷新数据")
    evidence = []
    def add(key, metric, value, unit, observed, source, **extra):
        evidence.append({"id": key, "metric": metric, "value": value, "unit": unit,
                         "observed_on": observed, "source": source, **extra})
    all_rows = build_period_rows(financial["reports"], [], [], dividend_events=dividends or None)
    fields = ("revenue", "profit", "revenue_growth", "profit_growth", "gross_margin", "net_margin",
              "roe", "roic", "operating_cash_flow", "capex", "free_cash_flow", "cash_dividend",
              "dividend_payout_ratio", "monetary_funds", "interest_bearing_debt", "net_cash")
    for period, limit in (("quarter", 12), ("ttm", 8), ("year", 5)):
        for row in view_rows(all_rows, period)[-limit:]:
            values = {field: row.get(field) for field in fields}
            # core treats absent debt components as zero; analysis cannot assume that.
            if any(row.get(field) is None for field in DEBT_COMPONENTS):
                values["interest_bearing_debt"] = values["net_cash"] = None
            add(f"financial.{period}.{row['period']}", "financial_period", values, "金额:元;比率:%",
                row["period"], "sina/legacy/manual", period_type=period,
                published_on=row.get("publish_date"),
                methodology="单季拆分/TTM/ROE/ROIC复用现有计算；自由现金流和债务为简化口径，时点值不加总")
    observation_rows = valuation.get("observation_rows") or valuation.get("rows", [])
    yield_rows = merge_dividend_yields([], monthly_dividend_yields(raw, dividends))
    for metric in ("pe", "pb", "ps", "dividend_yield"):
        for years in (3, 5, 10):
            summary = valuation_summary_observations(yield_rows if metric == "dividend_yield" else observation_rows, metric, years, valuation.get("industry", {}),
                as_of=today, trading_dates=[r["date"] for r in raw])
            values = {k: v for k, v in summary.items() if k not in {"rows", "rows_by_frequency"}}
            add(f"valuation.{metric}.{years}y", metric, summary["current"], "%" if metric == "dividend_yield" else "倍",
                summary["current_date"], valuation.get("basis"), years=years, **{k: v for k, v in values.items()
                    if k not in {"current", "current_date"}}, methodology="历史分位不代表内在价值；负PE不进入正PE分位")
    pe_observations = [r for r in observation_rows if r.get("pe_raw") is not None]
    if pe_observations:
        latest_pe = pe_observations[-1]
        add("valuation.pe.raw.latest", "pe_raw", latest_pe["pe_raw"], "倍", latest_pe["date"], valuation.get("basis"),
            methodology="负PE代表亏损或盈利口径不适用，不能以旧正PE代替当前PE")
    if raw:
        add("price.raw.latest", "close", raw[-1]["close"], "元/股", raw[-1]["date"], "tencent", adjustment="raw")
    if qfq:
        add("price.qfq.trend", "price_trend", [{"date": r["date"], "close": r["close"]} for r in qfq[-61:]],
            "元/股", qfq[-1]["date"], "tencent", adjustment="qfq")
    for i, row in enumerate(holders):
        earlier = next((r for r in reversed(holders[:i]) if row["holder_scope"] != "unknown" and r["holder_scope"] == row["holder_scope"]
                        and r["source"] == row["source"] and r["stat_date"] < row["stat_date"]), None)
        add(f"shareholders.{i}", "holders", row["holders"], "户", row["stat_date"], row["source"],
            scope=row["holder_scope"], announced_on=row["announced_on"],
            change=row["holders"] - earlier["holders"] if earlier else None)
    # Require actual known raw trading days; never bridge missing observations.
    financing_by_day = {r["trade_date"]: r for r in financing}
    known_days = [r["date"] for r in raw]
    if financing:
        latest = financing[-1]
        windows = {}
        for size in (5, 20, 60):
            dates = [d for d in known_days if d <= latest["trade_date"]][-(size + 1):]
            records = [financing_by_day.get(d) for d in dates]
            complete = len(records) == size + 1 and all(records)
            net_values = [r.get("net_buy") for r in records[1:]] if complete else []
            windows[str(size)] = {"sample_count": sum(r is not None for r in records), "complete": complete,
                "balance_change": records[-1]["margin_balance"] - records[0]["margin_balance"] if complete else None,
                "net_buy": sum(net_values) if complete and all(v is not None for v in net_values) else None}
        add("financing.summary", "financing", {"latest_balance": latest["margin_balance"], "windows": windows,
            "observations": [{k: r[k] for k in ("trade_date", "margin_balance", "net_buy")} for r in financing[-120:]]},
            "元", latest["trade_date"], latest["source"], methodology="融资流入不代表确定上涨")
    industry = industry_rows[-1] if industry_rows else None
    limitations = ["价值投资，中长期1–3年；仅使用本地事实，未查询新闻和公告。",
        "历史分位不等于内在价值；负PE不能解释为便宜；高股息率需核实可持续性。",
        "ROE/ROIC、债务及自由现金流采用简化口径；周期企业需正常周期盈利，金融业需专门核实适用性。",
        "股东人数不能识别机构身份；融资仅作背景。业务日期与抓取日期分开，节假日或停牌不能仅按日历判定过期。"]
    latest = all_rows[-1]
    required = ("revenue_ttm", "profit_ttm", "operating_cash_flow_ttm")
    missing = [field for field in required if latest.get(field) is None]
    if len(all_rows) < 8:
        missing.append("经营趋势历史不足")
    usable_valuation = any(e["id"].startswith("valuation.") and e["metric"] != "dividend_yield"
                           and e["value"] is not None for e in evidence)
    if not usable_valuation:
        missing.append("适用估值证据不足")
    if not holders:
        limitations.append("股东人数资料缺失")
    if not financing:
        limitations.append("融资资料缺失")
    if not dividends:
        limitations.append("分红实施资料缺失，不能推定未来零分红")
    if not industry:
        limitations.append("行业未知，需核实通用经营/现金流/债务指标适用性")
    source_errors = recent_failures
    quality = {"missing": missing, "limited": bool(missing or not holders or not financing or not dividends or not industry),
               "candidate_allowed": not missing and bool(industry), "source_status": states,
               "source_errors": source_errors, "updating": bool(active_updates),
               "dates": {"financial": latest["period"], "price": raw[-1]["date"] if raw else None,
                         "valuation": max((r["date"] for r in observation_rows), default=None),
                         "shareholders": holders[-1]["stat_date"] if holders else None,
                         "financing": financing[-1]["trade_date"] if financing else None}}
    input_data = {"schema_version": INPUT_VERSION, "calculation_version": CALCULATION_VERSION,
        "instrument": {"code": code, "name": instrument["name"], "industry": industry["industry_name"] if industry else None,
                       "industry_source": industry["source"] if industry else None,
                       "industry_report_period": json.loads(industry["raw_json"]).get("REPORT_DATE") if industry else None,
                       "industry_basis": industry["classification_basis"] if industry else None},
        "analysis_profile": PROFILE, "evidence": evidence, "limitations": limitations,
        "quality": {**{k: quality[k] for k in ("missing", "limited", "candidate_allowed", "dates")},
                    "source_failures": [{k: r[k] for k in ("dataset", "source", "status")} for r in source_errors]}}
    # Keep sufficient baselines and source records, rather than the whole database.
    earliest_financial = view_rows(all_rows, "year")[-6:][0]["period"] if view_rows(all_rows, "year") else all_rows[0]["period"]
    cutoff = f"{int(today[:4]) - 10}{today[4:]}"
    manifest["financial"] = [r for r in manifest["financial"] if r["period"] >= earliest_financial]
    manifest["overrides"] = [r for r in manifest["overrides"] if r["period"] >= earliest_financial]
    manifest["valuation"] = [r for r in manifest["valuation"] if r["observed_on"] >= cutoff]
    manifest["raw_prices"] = [{"date": r["date"], "close": r["close"]} for r in raw if r["date"] >= cutoff]
    manifest["qfq_prices"] = [{"date": r["date"], "close": r["close"]} for r in qfq[-61:]]
    # Traceability may include refresh times/run IDs; actual inference input never does.
    return {"instrument_id": identity, "input": input_data, "hash": digest(input_data),
            "manifest": manifest, "quality": quality, "captured_at": utc_now()}


def changes(old, new):
    groups = {"financial": "财报/手工覆盖", "valuation": "估值", "price": "价格",
              "shareholders": "股东人数", "financing": "融资"}
    previous = {e["id"]: e for e in old.get("evidence", [])}
    current = {e["id"]: e for e in new.get("evidence", [])}
    changed = {key.split(".")[0] for key in previous.keys() | current.keys() if previous.get(key) != current.get(key)}
    result = [label for group, label in groups.items() if group in changed]
    if not result and old != new:
        result.append("口径/质量/行业")
    return result
