"""Monthly valuation series and comparable-industry snapshot."""

from __future__ import annotations

import math
from bisect import bisect_right
from datetime import date, timedelta

import requests

from .core import build_period_rows
from .sources import fetch_daily_prices, normalize_code, symbol_for


BAIDU_URL = "https://gushitong.baidu.com/opendata"
EASTMONEY_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
DIVIDEND_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
BAIDU_INDICATORS = {"pe": "市盈率(TTM)", "pb": "市净率", "market_cap": "总市值"}


def _finite_number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _positive(value):
    result = _finite_number(value)
    return result if result is not None and result > 0 else None


def _nonnegative(value):
    result = _finite_number(value)
    return result if result is not None and result >= 0 else None


def fetch_valuation_series(code: str, session: requests.Session) -> dict[str, list[dict]]:
    """Baidu's latest ten-year PE/PB/capitalization series (cap in 亿元)."""
    code = normalize_code(code)
    result = {}
    for key, indicator in BAIDU_INDICATORS.items():
        response = session.get(BAIDU_URL, params={
            "openapi": "1", "dspName": "iphone", "tn": "tangram", "client": "app",
            "query": indicator, "code": code, "word": "", "resource_id": "51171",
            "market": "ab", "tag": indicator, "chart_select": "近十年",
            "industry_select": "", "skip_industry": "1", "finClientType": "pc",
        }, timeout=20)
        response.raise_for_status()
        try:
            chart = response.json()["Result"][0]["DisplayData"]["resultData"]["tplData"]["result"]["chartInfo"][0]
            body = chart["body"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"百度未返回 {indicator} 历史序列") from exc
        if not body:
            raise ValueError(f"百度未返回 {indicator} 历史序列")
        points = []
        for raw_date, raw_value in body:
            date.fromisoformat(raw_date)
            points.append({"date": raw_date, "value": _finite_number(raw_value)})
        result[key] = points
    return result


def fetch_industry_snapshot(code: str, session: requests.Session) -> dict:
    """EastMoney's current peer-group arithmetic average, not a historical series."""
    symbol = symbol_for(code)
    security = f"{code}.{symbol[:2].upper()}"
    response = session.get(EASTMONEY_URL, params={
        "reportName": "RPT_PCF10_INDUSTRY_CVALUE", "columns": "ALL",
        "filter": f'(SECUCODE="{security}")', "pageNumber": "", "pageSize": "",
        "sortTypes": "1", "sortColumns": "PAIMING", "source": "HSF10", "client": "PC",
    }, headers={"Referer": "https://emweb.securities.eastmoney.com/"}, timeout=20)
    response.raise_for_status()
    try:
        rows = response.json()["result"]["data"]
        average = next(row for row in rows if row.get("CORRE_SECURITY_CODE") == "行业平均")
    except (KeyError, StopIteration, TypeError) as exc:
        raise ValueError("东方财富未返回同行业平均估值") from exc
    return {"pe": _positive(average.get("PE_TTM")),
            "pb": _positive(average.get("PB_MRQ")),
            "ps": _positive(average.get("PS_TTM")),
            "peer_count": average.get("TOTAL_COUNT"),
            "report_period": str(average.get("REPORT_DATE") or "")[:10] or None}


def fetch_dividend_events(code: str, session: requests.Session) -> list[dict]:
    """Implemented cash distributions; EastMoney stores PRETAX_BONUS_RMB per ten shares."""
    response = session.get(DIVIDEND_URL, params={
        "reportName": "RPT_SHAREBONUS_DET", "columns": "ALL",
        "filter": f'(SECURITY_CODE="{normalize_code(code)}")',
        "pageNumber": "1", "pageSize": "200", "sortTypes": "-1",
        "sortColumns": "EX_DIVIDEND_DATE", "source": "WEB", "client": "WEB",
    }, headers={"Referer": "https://data.eastmoney.com/", "User-Agent": "Mozilla/5.0"}, timeout=20)
    response.raise_for_status()
    try:
        result = response.json()["result"]
        records = result["data"]
        if int(result.get("count") or 0) > len(records):
            raise ValueError("分红记录超过单次查询上限")
    except (KeyError, TypeError) as exc:
        raise ValueError("东方财富未返回分红记录") from exc
    events = []
    for record in records:
        ex_date = str(record.get("EX_DIVIDEND_DATE") or "")[:10]
        cash_per_ten = _positive(record.get("PRETAX_BONUS_RMB"))
        if ex_date and cash_per_ten is not None:
            date.fromisoformat(ex_date)
            events.append({"date": ex_date, "per_share": cash_per_ten / 10})
    return sorted(events, key=lambda event: event["date"])


def monthly_dividend_yields(raw_prices: list[dict], events: list[dict]) -> list[dict]:
    """Trailing 365-day paid dividends / actual unadjusted month close, in percent."""
    last_price = {}
    for price in raw_prices:
        key = price["date"][:7]
        if key not in last_price or price["date"] > last_price[key]["date"]:
            last_price[key] = price
    result = []
    for month in sorted(last_price):
        price = last_price[month]
        close = _positive(price.get("close"))
        if close is None:
            continue
        end = date.fromisoformat(price["date"])
        start = end - timedelta(days=365)
        cash = sum(event["per_share"] for event in events
                   if start < date.fromisoformat(event["date"]) <= end)
        result.append({"date": price["date"], "value": cash / close * 100})
    return result


def fetch_dividend_yields(code: str, session: requests.Session) -> list[dict]:
    events = fetch_dividend_events(code, session)
    today = date.today()
    try:
        earliest = today.replace(year=today.year - 10)
    except ValueError:
        earliest = today.replace(year=today.year - 10, day=28)
    prices = fetch_daily_prices(code, session, "", earliest.isoformat(), today.isoformat())
    if not prices:
        raise ValueError("腾讯未返回未复权历史价格")
    return monthly_dividend_yields(prices, events)


def merge_dividend_yields(rows: list[dict], yields: list[dict]) -> list[dict]:
    by_month = {row["date"][:7]: dict(row) for row in rows}
    for point in yields:
        month = point["date"][:7]
        row = by_month.setdefault(month, {"date": point["date"], "pe": None,
                                          "pb": None, "ps": None})
        row["date"] = max(row["date"], point["date"])
        row["dividend_yield"] = point["value"]
        row["dividend_yield_date"] = point["date"]
    return [by_month[month] for month in sorted(by_month)]


def monthly_valuation(series: dict[str, list[dict]], reports: list[dict]) -> list[dict]:
    """Use each month's last observation and only reports disclosed by that date."""
    last_in_month = {}
    for metric, points in series.items():
        for point in points:
            key = (metric, point["date"][:7])
            if key not in last_in_month or point["date"] > last_in_month[key]["date"]:
                last_in_month[key] = point
    months = sorted({month for _, month in last_in_month})
    financial = sorted(
        ((row["publish_date"], row["period"], row["revenue_ttm"])
         for row in build_period_rows(reports, [], []) if row.get("publish_date")),
        key=lambda item: (item[0], item[1]),
    )
    disclosure_dates = [item[0] for item in financial]
    result = []
    for month in months:
        observations = {metric: last_in_month.get((metric, month)) for metric in ("pe", "pb", "market_cap")}
        latest_date = max((point["date"] for point in observations.values() if point), default=None)
        if latest_date is None:
            continue
        cap = observations["market_cap"]
        ps = None
        if cap:
            index = bisect_right(disclosure_dates, cap["date"]) - 1
            if index >= 0:
                publish_date, _, revenue_ttm = financial[index]
                age = (date.fromisoformat(cap["date"]) - date.fromisoformat(publish_date)).days
                if revenue_ttm and revenue_ttm > 0 and age <= 400:
                    ps = _positive(cap["value"] * 1e8 / revenue_ttm)
        result.append({"date": latest_date,
                       "pe": _positive(observations["pe"]["value"]) if observations["pe"] else None,
                       "pb": _positive(observations["pb"]["value"]) if observations["pb"] else None,
                       "ps": ps})
    return result


def _percentile(values: list[float], fraction: float) -> float:
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def valuation_summary(rows: list[dict], metric: str, years: int, industry: dict,
                      as_of: str | None = None) -> dict:
    if metric not in ("pe", "pb", "ps", "dividend_yield") or years not in (3, 5, 10):
        raise ValueError("不支持的估值指标或时间范围")
    end = date.fromisoformat(as_of) if as_of else date.today()
    try:
        start = end.replace(year=end.year - years)
    except ValueError:  # February 29
        start = end.replace(year=end.year - years, day=28)
    window = [row for row in rows if start.isoformat() <= row["date"] <= end.isoformat()]
    valid_number = _nonnegative if metric == "dividend_yield" else _positive
    valid = [row for row in window if valid_number(row.get(metric)) is not None]
    values = sorted(float(row[metric]) for row in valid)
    current = valid_number(window[-1].get(metric)) if window else None
    benchmark = _positive(industry.get(metric))
    return {
        "rows": window, "count": len(values), "current": current,
        "current_date": window[-1].get("dividend_yield_date", window[-1]["date"])
        if current is not None else None,
        "high": _percentile(values, .8) if values else None,
        "median": _percentile(values, .5) if values else None,
        "low": _percentile(values, .2) if values else None,
        "percentile": round(100 * sum(value < current for value in values) / len(values), 1) if current is not None else None,
        "industry": benchmark,
        "industry_relation": ("higher" if current > benchmark else "lower" if current < benchmark else "equal")
        if current is not None and benchmark is not None else None,
    }
