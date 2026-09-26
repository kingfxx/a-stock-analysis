"""Public-source adapters for quarterly reports and disclosure-date prices."""

from __future__ import annotations

import math
import re
from datetime import date, timedelta

import requests


SINA_URL = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
PROFIT_KEYS = (
    "归属于母公司所有者的净利润", "归属于母公司的净利润",
    "归属于母公司股东的净利润", "归属于上市公司股东的净利润",
)
SHARE_KEYS = ("实收资本(或股本)", "实收资本（或股本）", "股本")
EQUITY_KEYS = ("归属于母公司股东权益合计", "归属于母公司股东的权益", "归属于母公司所有者权益合计")


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


def _date(value):
    text = str(value or "")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if re.fullmatch(r"\d{8}", text) else None


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
        fields = {item.get("item_title"): item.get("item_value") for item in record.get("data", [])}
        counterpart = balance.get(key, {})
        balance_fields = {item.get("item_title"): item.get("item_value") for item in counterpart.get("data", [])}
        output.append({
            "period": period,
            "publish_date": _date(record.get("publish_date")),
            "revenue_ytd": _first(fields, ("营业收入", "营业总收入")),
            "profit_ytd": _first(fields, PROFIT_KEYS),
            "shares": _first(balance_fields, SHARE_KEYS),
            "equity": _first(balance_fields, EQUITY_KEYS),
            "source_update_time": record.get("update_time"),
        })
    if not output:
        raise ValueError("新浪利润表报告期无法解析")
    return sorted(output, key=lambda row: row["period"])


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


def fetch_stock_name(code: str, session: requests.Session) -> str:
    symbol = symbol_for(code)
    response = session.get(TENCENT_QUOTE_URL + symbol, timeout=12)
    response.raise_for_status()
    return parse_stock_name(response.content.decode("gbk", errors="replace"), symbol)


def fetch_financial_reports(code: str, session: requests.Session) -> list[dict]:
    symbol = symbol_for(code)

    def one(kind):
        response = session.get(SINA_URL, params={"paperCode": symbol, "source": kind,
                                                 "type": "0", "page": "1", "num": "200"}, timeout=18)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("result", {}).get("data", {}) or {}
        count = int(data.get("report_count") or 0)
        periods = data.get("report_list", {}) or {}
        if count > len(periods):
            raise ValueError(f"新浪{kind}只返回 {len(periods)}/{count} 期，历史不完整")
        return payload

    return parse_financial_reports(one("lrb"), one("fzb"))


def fetch_monthly_prices(code: str, session: requests.Session, adjust: str = "") -> list[dict]:
    if adjust not in ("", "qfq"):
        raise ValueError("仅支持未复权或前复权月 K")
    symbol = symbol_for(code)
    response = session.get(TENCENT_URL, params={"param": f"{symbol},month,,,640,{adjust}"},
                           headers={"Referer": "https://gu.qq.com/"}, timeout=18)
    response.raise_for_status()
    return parse_monthly_prices(response.json(), symbol, adjust)


def fetch_daily_prices(code: str, session: requests.Session, adjust: str,
                       earliest_date: str, latest_date: str) -> list[dict]:
    """Page backward through Tencent daily K; callers retain only report-date snapshots."""
    if adjust not in ("", "qfq"):
        raise ValueError("仅支持未复权或前复权日 K")
    symbol = symbol_for(code)
    end = date.fromisoformat(latest_date)
    earliest = date.fromisoformat(earliest_date)
    prices = {}
    for _ in range(50):
        if end < earliest:
            break
        response = session.get(TENCENT_URL,
                               params={"param": f"{symbol},day,,{end.isoformat()},640,{adjust}"},
                               headers={"Referer": "https://gu.qq.com/"}, timeout=18)
        response.raise_for_status()
        batch = parse_daily_prices(response.json(), symbol, adjust)
        if not batch:
            break
        prices.update({row["date"]: row for row in batch})
        oldest = min(date.fromisoformat(row["date"]) for row in batch)
        if oldest > end:
            raise ValueError("腾讯日 K 分页没有向历史推进")
        end = oldest - timedelta(days=1)
        if len(batch) < 640:
            break
    else:
        raise ValueError("腾讯日 K 历史分页超过 50 次")
    return [prices[key] for key in sorted(prices)]
