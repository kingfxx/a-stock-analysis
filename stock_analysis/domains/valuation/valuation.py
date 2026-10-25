"""Monthly valuation series and comparable-industry snapshot."""

from __future__ import annotations

import math
from datetime import date, timedelta

import requests

from stock_analysis.domains.valuation.calculations import (_finite_number, _positive, _nonnegative, cash_dividend_per_share_365, monthly_dividend_yields, daily_dividend_yield_observations, merge_dividend_yields, merge_adjusted_prices, monthly_valuation, _percentile, _standard_deviation_benchmark, valuation_summary, valuation_observation_rows, aggregate_valuation_rows, valuation_summary_observations)
from stock_analysis.domains.stocks.sources import fetch_daily_prices, normalize_code, symbol_for


BAIDU_URL = "https://gushitong.baidu.com/opendata"
EASTMONEY_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
DIVIDEND_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
BAIDU_INDICATORS = {"pe": "市盈率(TTM)", "pb": "市净率", "market_cap": "总市值"}
BAIDU_WINDOWS = ("近十年", "近五年", "近三年", "近一年")


def fetch_valuation_indicator(code: str, session: requests.Session, metric: str,
                              window: str = "近十年") -> list[dict]:
    """One Baidu metric and source window; capitalization values are in 亿元."""
    code = normalize_code(code)
    if metric not in BAIDU_INDICATORS or window not in BAIDU_WINDOWS:
        raise ValueError("不支持的百度估值指标或时间范围")
    indicator = BAIDU_INDICATORS[metric]
    response = session.get(BAIDU_URL, params={
        "openapi": "1", "dspName": "iphone", "tn": "tangram", "client": "app",
        "query": indicator, "code": code, "word": "", "resource_id": "51171",
        "market": "ab", "tag": indicator, "chart_select": window,
        "industry_select": "", "skip_industry": "1", "finClientType": "pc",
    }, timeout=20)
    response.raise_for_status()
    try:
        chart = response.json()["Result"][0]["DisplayData"]["resultData"]["tplData"]["result"]["chartInfo"][0]
        body = chart["body"]
        if chart.get("type", window) not in (window, "全部") or chart.get("header", [indicator])[0] != indicator:
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"百度未返回 {indicator} {window} 历史序列") from exc
    if not body:
        raise ValueError(f"百度未返回 {indicator} {window} 历史序列")
    points = []
    seen = set()
    for raw_date, raw_value in body:
        date.fromisoformat(raw_date)
        if raw_date in seen:
            raise ValueError("百度估值序列存在重复日期")
        seen.add(raw_date)
        point = {"date": raw_date, "value": _finite_number(raw_value)}
        if chart.get("type") == "全部":
            point.update(source_window="全部", requested_window=window)
        points.append(point)
    return points


def fetch_valuation_series(code: str, session: requests.Session,
                           window: str = "近十年") -> dict[str, list[dict]]:
    """Fetch all three Baidu metrics; defaults to the original ten-year API."""
    return {metric: fetch_valuation_indicator(code, session, metric, window)
            for metric in BAIDU_INDICATORS}


def fetch_industry_snapshot(code: str, session: requests.Session) -> dict:
    """EastMoney's current peer-group arithmetic average, not a historical series."""
    return fetch_industry_snapshot_details(code, session)["snapshot"]


def fetch_industry_snapshot_details(code: str, session: requests.Session) -> dict:
    """Keep the source record alongside its normalized peer average."""
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
    snapshot = {"pe": _positive(average.get("PE_TTM")),
                "pb": _positive(average.get("PB_MRQ")),
                "ps": _positive(average.get("PS_TTM")),
                "peer_count": average.get("TOTAL_COUNT"),
                "report_period": str(average.get("REPORT_DATE") or "")[:10] or None}
    return {"snapshot": snapshot, "raw": average,
            "industry_name": str(average.get("INDUSTRY_NAME") or average.get("HYMC") or "未知行业"),
            "industry_code": average.get("INDUSTRY_CODE") or average.get("HYDM")}


def fetch_dividend_event_page(code: str, session: requests.Session, *,
                              date_field: str | None = None, since: str | None = None,
                              page: int = 1, page_size: int = 200,
                              report_period: str | None = None) -> dict:
    """Return one EastMoney page without discarding proposals or source fields."""
    code = normalize_code(code)
    if ((date_field is None) != (since is None) or
            date_field not in (None, "NOTICE_DATE", "PLAN_NOTICE_DATE", "EX_DIVIDEND_DATE") or
            not isinstance(page, int) or page < 1 or
            not isinstance(page_size, int) or page_size < 1):
        raise ValueError("分红分页或日期过滤参数无效")
    if since is not None:
        date.fromisoformat(since)
    if report_period is not None:
        date.fromisoformat(report_period)
    filter_text = f'(SECURITY_CODE="{code}")'
    if date_field:
        filter_text += f"({date_field}>='{since}')"
    if report_period:
        filter_text += f'(REPORT_DATE="{report_period}")'
    response = session.get(DIVIDEND_URL, params={
        "reportName": "RPT_SHAREBONUS_DET", "columns": "ALL",
        "filter": filter_text, "pageNumber": str(page), "pageSize": str(page_size),
        "sortTypes": "-1", "sortColumns": "EX_DIVIDEND_DATE",
        "source": "WEB", "client": "WEB",
    }, headers={"Referer": "https://data.eastmoney.com/", "User-Agent": "Mozilla/5.0"}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if page == 1 and payload.get("code") == 9201 and payload.get("result") is None:
        return {"records": [], "total": 0, "pages": 0}
    try:
        if payload.get("success") is not True:
            raise ValueError
        result = payload["result"]
        records = result["data"]
        total = int(result["count"])
        pages = int(result["pages"])
        expected = min(page_size, max(0, total - (page - 1) * page_size))
        if (total < 0 or pages < 0 or not isinstance(records, list) or len(records) != expected or
                (total and pages != math.ceil(total / page_size)) or
                (not total and pages not in (0, 1))):
            raise ValueError
        for record in records:
            if not isinstance(record, dict) or record.get("SECURITY_CODE") != code:
                raise ValueError
            if date_field:
                raw_date = str(record.get(date_field) or "")[:10]
                if not raw_date or date.fromisoformat(raw_date).isoformat() < since:
                    raise ValueError
            if report_period:
                raw_period = str(record.get("REPORT_DATE") or "")[:10]
                if not raw_period or date.fromisoformat(raw_period).isoformat() != report_period:
                    raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("东方财富分红分页数据不完整或忽略过滤") from exc
    return {"records": records, "total": total, "pages": pages}


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
        report_period = str(record.get("REPORT_DATE") or "")[:10]
        cash_per_ten = _positive(record.get("PRETAX_BONUS_RMB"))
        if ex_date and cash_per_ten is not None:
            date.fromisoformat(ex_date)
            if ex_date > date.today().isoformat():
                continue
            if report_period:
                date.fromisoformat(report_period)
            events.append({"date": ex_date, "report_period": report_period or None,
                           "per_share": cash_per_ten / 10,
                           "total_shares": _positive(record.get("TOTAL_SHARES"))})
    return sorted(events, key=lambda event: event["date"])


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
