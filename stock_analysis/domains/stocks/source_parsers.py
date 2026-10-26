"""Pure Sina financial and Tencent quote/price parsing. No network or storage dependencies."""
from __future__ import annotations

import math
import json
import re
from decimal import Decimal
from datetime import date, timedelta

PROFIT_KEYS = (
    "归属于母公司所有者的净利润", "归属于母公司的净利润",
    "归属于母公司股东的净利润", "归属于上市公司股东的净利润",
)


ADJUSTED_PROFIT_KEYS = ("扣除非经常性损益后的归属于母公司所有者的净利润", "扣除非经常性损益后的归属于母公司股东的净利润", "扣非归母净利润")


SHARE_KEYS = ("实收资本(或股本)", "实收资本（或股本）", "股本")


EQUITY_KEYS = ("归属于母公司股东权益合计", "归属于母公司股东的权益", "归属于母公司所有者权益合计")


TOTAL_EQUITY_KEYS = ("所有者权益(或股东权益)合计", "所有者权益（或股东权益）合计",
                     "所有者权益合计", "股东权益合计")


CASH_FLOW_KEYS = ("经营活动产生的现金流量净额",)


CAPEX_KEYS = ("购建固定资产、无形资产和其他长期资产所支付的现金",
              "购建固定资产、无形资产和其他长期资产支付的现金")


DEBT_FIELDS = {
    "short_term_borrowings": ("短期借款",),
    "short_term_bonds": ("应付短期债券",),
    "current_noncurrent_liabilities": ("一年内到期的非流动负债",),
    "long_term_borrowings": ("长期借款",),
    "bonds_payable": ("应付债券",),
    "lease_liabilities": ("租赁负债",),
}


def _number(value):
    if value in (None, "", "--"):
        return None
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _first(fields: dict, names: tuple[str, ...]):
    for name in names:
        if name in fields:
            value = _number(fields[name])
            if value is not None:
                return value
    return None


def _financing_interest_income(items: list[dict]):
    # Sina also has INTEINCO (operating interest revenue of finance subsidiaries).
    # Only INTEINCOOPCOST belongs to the finance-expense breakdown used by ROIC.
    for item in items:
        if item.get("item_field") == "INTEINCOOPCOST":
            return _number(item.get("item_value"))
    in_finance_expenses = False
    for item in items:
        title = item.get("item_title")
        if title == "财务费用":
            in_finance_expenses = True
        elif title in ("利息支出", "营业利润", "投资收益", "其他收益"):
            in_finance_expenses = False
        elif in_finance_expenses and title == "利息收入":
            return _number(item.get("item_value"))
    return None


def _date(value):
    text = str(value or "")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if re.fullmatch(r"\d{8}", text) else None


def normalize_report_dates(reports: list[dict]) -> list[dict]:
    """Correct Sina's historical publish_date shift using the prior year's same period."""
    by_period = {report["period"]: report for report in reports}

    def source_date(report):
        return report.get("source_publish_date", report.get("publish_date")) if report else None

    def plausible(period, published):
        if not published:
            return False
        try:
            delay = (date.fromisoformat(published) - date.fromisoformat(period)).days
        except ValueError:
            return False
        return 0 <= delay <= 366

    corrected = []
    for report in reports:
        period = report["period"]
        previous = by_period.get(f"{int(period[:4]) - 1}{period[4:]}")
        candidate = source_date(previous)
        raw = source_date(report)
        published = (report.get("publish_date") if report.get("_publish_date_override_applied") or report.get("_publish_date_finalized")
                     else candidate if plausible(period, candidate) else raw if plausible(period, raw) else None)
        corrected.append({**report, "source_publish_date": raw, "publish_date": published})
    return corrected


def parse_financial_reports(income_payload: dict, balance_payload: dict) -> list[dict]:
    income = (income_payload.get("result", {}).get("data", {}) or {}).get("report_list", {}) or {}
    balance = (balance_payload.get("result", {}).get("data", {}) or {}).get("report_list", {}) or {}
    if not income:
        raise ValueError("新浪利润表没有报告期记录")
    output = []
    for key, record in income.items():
        period = _date(key)
        if period is None:
            continue
        items = record.get("data", [])
        fields = {item.get("item_title"): item.get("item_value") for item in items}
        counterpart = balance.get(key, {})
        balance_fields = {item.get("item_title"): item.get("item_value") for item in counterpart.get("data", [])}
        debt_fields = {field: _first(balance_fields, names) for field, names in DEBT_FIELDS.items()
                       if any(name in balance_fields for name in names)}
        interest_expense = _first(fields, ("利息费用",))
        # Expense-only item: Sina preserves credit/debit display signs in some
        # issuers (e.g. Midea), while others report positive expense magnitudes.
        # Normalize this specific cost at ingestion, never finance expenses or
        # operating "利息支出", taxes, interest income, or the calculated result.
        if interest_expense is not None and interest_expense < 0:
            interest_expense = -interest_expense
        output.append({
            "period": period,
            "publish_date": _date(record.get("publish_date")),
            "revenue_ytd": _first(fields, ("营业收入", "营业总收入")),
            "profit_ytd": _first(fields, PROFIT_KEYS),
            "adjusted_profit_ytd": _first(fields, ADJUSTED_PROFIT_KEYS),
            "operating_cost_ytd": _first(fields, ("营业成本",)),
            "net_profit_ytd": _first(fields, ("净利润",)),
            "profit_before_tax_ytd": _first(fields, ("利润总额",)),
            "income_tax_expense_ytd": _first(fields, ("所得税费用",)),
            "interest_expense_ytd": interest_expense,
            "non_operating_interest_income_ytd": _financing_interest_income(items),
            "total_equity": _first(balance_fields, TOTAL_EQUITY_KEYS),
            "monetary_funds": _first(balance_fields, ("货币资金",)),
            **debt_fields,
            "shares": _first(balance_fields, SHARE_KEYS),
            "equity": _first(balance_fields, EQUITY_KEYS),
            "source_update_time": record.get("update_time"),
        })
    if not output:
        raise ValueError("新浪利润表报告期无法解析")
    return normalize_report_dates(sorted(output, key=lambda row: row["period"]))


def parse_adjusted_profit_reports(records: list[dict]) -> dict[str, float]:
    """Read applicable adjusted parent profit from Sina's gjzb NPCUT field."""
    values = {}
    for record in records:
        raw = record.get("raw_json")
        if not raw:
            continue
        payload = json.loads(raw)
        items = payload.get("data", []) if isinstance(payload, dict) else []
        value = next((_number(item.get("item_value")) for item in items
                      if item.get("item_field") == "NPCUT" and _number(item.get("item_value")) is not None), None)
        if value is not None:
            values[record["period"]] = value
    return values


def parse_cash_flow_reports(payload: dict) -> dict[str, dict]:
    periods = (payload.get("result", {}).get("data", {}) or {}).get("report_list", {}) or {}
    if not periods:
        raise ValueError("新浪现金流量表没有报告期记录")
    result = {}
    for key, record in periods.items():
        period = _date(key)
        if period is None:
            continue
        fields = {item.get("item_title"): item.get("item_value") for item in record.get("data", [])}
        result[period] = {"operating_cash_flow_ytd": _first(fields, CASH_FLOW_KEYS),
                          "main_business_cash_flow_ytd": _first(fields, ("销售商品、提供劳务收到的现金",)),
                          "capex_ytd": _first(fields, CAPEX_KEYS)}
    if not result:
        raise ValueError("新浪现金流量表报告期无法解析")
    return result


def parse_monthly_prices(payload: dict, symbol: str, adjust: str) -> list[dict]:
    if payload.get("code") != 0:
        raise ValueError(f"腾讯月 K 请求失败：{payload.get('msg', payload.get('code'))}")
    node = (payload.get("data") or {}).get(symbol) or {}
    key = f"{adjust}month"
    records = node.get(key, node.get("month") if not adjust else None)
    if not records:
        raise ValueError(f"腾讯月 K 未返回 {key} 数据")
    output = []
    for record in records:
        if len(record) < 3 or _number(record[2]) is None:
            raise ValueError("腾讯月 K 字段格式不完整")
        output.append({"date": record[0], "close": float(record[2])})
    return output


def parse_daily_prices(payload: dict, symbol: str, adjust: str) -> list[dict]:
    if payload.get("code") != 0:
        raise ValueError(f"腾讯日 K 请求失败：{payload.get('msg', payload.get('code'))}")
    node = (payload.get("data") or {}).get(symbol) or {}
    records = node.get(f"{adjust}day")
    if records is None:
        raise ValueError(f"腾讯日 K 未返回 {adjust}day 数据")
    output = []
    for record in records:
        if len(record) < 3 or _number(record[2]) is None:
            raise ValueError("腾讯日 K 字段格式不完整")
        date.fromisoformat(record[0])
        output.append({"date": record[0], "close": float(record[2])})
    return output


def normalize_code(code: str) -> str:
    code = code.strip().lower()
    if code.startswith(("sh", "sz")):
        code = code[2:]
    if not re.fullmatch(r"\d{6}", code) or code[0] not in "036":
        raise ValueError("请输入沪深 A 股六位代码，例如 300750")
    return code


def symbol_for(code: str) -> str:
    code = normalize_code(code)
    return ("sh" if code.startswith("6") else "sz") + code


def parse_stock_name(text: str, symbol: str) -> str:
    match = re.fullmatch(rf'v_{re.escape(symbol)}="([^"]*)";?\s*', text.strip())
    if not match:
        raise ValueError("腾讯行情名称响应格式不正确")
    fields = match.group(1).split("~")
    if len(fields) < 3 or fields[2] != symbol[2:] or not fields[1].strip():
        raise ValueError("腾讯行情名称与股票代码不匹配")
    return fields[1].strip()


def parse_price_history(payload: dict, symbol: str, adjust: str) -> list[dict]:
    """Full source record, including OHLC; adjusted history may be nonpositive."""
    if adjust not in ("", "qfq"):
        raise ValueError("不支持的复权口径")
    if payload.get("code") != 0:
        raise ValueError("腾讯行情响应失败")
    raw = (payload.get("data") or {}).get(symbol, {}).get(f"{adjust}day")
    if not isinstance(raw, list):
        raise ValueError("腾讯未返回正确股票/口径的日行情")
    rows = []
    seen = set()
    for record in raw:
        if not isinstance(record, list) or len(record) < 6:
            raise ValueError("腾讯日行情字段缺失")
        day = date.fromisoformat(record[0]).isoformat()
        values = [_number(v) for v in record[1:6]]
        if day in seen or any(v is None for v in values):
            raise ValueError("腾讯日行情存在重复日期或无效值")
        if values[4] < 0 or (not adjust and any(v <= 0 for v in values[:4])):
            raise ValueError("腾讯未复权价格或成交量无效")
        seen.add(day)
        rows.append(dict(date=day, open=values[0], close=values[1], high=values[2],
                         low=values[3], volume=values[4], raw=record))
    return sorted(rows, key=lambda r: r["date"])


def price_equal(first: dict, second: dict) -> bool:
    return all(Decimal(str(first[k])).quantize(Decimal("0.0001")) ==
               Decimal(str(second[k])).quantize(Decimal("0.0001"))
               for k in ("open", "close", "high", "low"))


def parse_financial_report_page(payload: dict, source: str, num: int, page: int) -> dict:
    try:
        result = payload["result"]
        if result.get("status", {}).get("code", 0) != 0:
            raise ValueError
        data = result["data"]
        total = int(data["report_count"])
        records = data["report_list"]
        if total < 0 or not isinstance(records, dict):
            raise ValueError
        periods = list(records)
        if len(periods) != min(num, max(0, total - (page - 1) * num)):
            raise ValueError
        if any(_date(period) is None or not isinstance(records[period], dict) or
               not isinstance(records[period].get("data"), list) for period in periods):
            raise ValueError
        for period in periods:
            date.fromisoformat(_date(period))
        if periods != sorted(periods, reverse=True):
            raise ValueError
        listed = data.get("report_date")
        if listed is not None and [item.get("date_value") for item in listed] != periods:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"新浪{source}分页数据不完整或无效") from exc
    return {"records": records, "total": total,
            "newest_period": _date(periods[0]) if periods else None,
            "oldest_period": _date(periods[-1]) if periods else None}


def validate_financial_history(payload: dict, kind: str) -> dict:
    data = payload.get("result", {}).get("data", {}) or {}
    count = int(data.get("report_count") or 0)
    periods = data.get("report_list", {}) or {}
    if count > len(periods):
        raise ValueError(f"新浪{kind}只返回 {len(periods)}/{count} 期，历史不完整")
    return payload



def parse_cash_flow_history(payload: dict) -> dict[str, dict]:
    data = payload.get("result", {}).get("data", {}) or {}
    periods = data.get("report_list", {}) or {}
    if int(data.get("report_count") or 0) > len(periods):
        raise ValueError(f"新浪llb只返回 {len(periods)}/{data['report_count']} 期，历史不完整")
    return parse_cash_flow_reports(payload)
