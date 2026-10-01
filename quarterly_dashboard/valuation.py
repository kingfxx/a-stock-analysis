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
BAIDU_WINDOWS = ("近十年", "近五年", "近三年", "近一年")


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
        if chart.get("type", window) != window or chart.get("header", [indicator])[0] != indicator:
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
        points.append({"date": raw_date, "value": _finite_number(raw_value)})
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


def merge_adjusted_prices(rows: list[dict], prices: list[dict]) -> list[dict]:
    """Join actual monthly qfq closes without substituting a nearby month's price."""
    by_month = {row["date"][:7]: dict(row) for row in rows}
    for price in prices:
        month = price["date"][:7]
        row = by_month.setdefault(month, {"date": price["date"], "pe": None,
                                          "pb": None, "ps": None})
        if price["date"] >= row.get("qfq_close_date", ""):
            row["qfq_close"] = _finite_number(price.get("close"))
            row["qfq_close_date"] = price["date"]
        row["date"] = max(row["date"], price["date"])
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
    window = sorted((row for row in rows if start.isoformat() <= row["date"] <= end.isoformat()),
                    key=lambda row: row["date"])
    valid_number = _nonnegative if metric == "dividend_yield" else _positive
    valid = [row for row in window if valid_number(row.get(metric)) is not None]
    values = sorted(float(row[metric]) for row in valid)
    latest = valid[-1] if valid else None
    current = valid_number(latest.get(metric)) if latest else None
    benchmark = _positive(industry.get(metric))
    return {
        "rows": window, "count": len(values), "current": current,
        "current_date": (latest.get(f"{metric}_date") or latest["date"]) if latest else None,
        "high": _percentile(values, .8) if values else None,
        "median": _percentile(values, .5) if values else None,
        "low": _percentile(values, .2) if values else None,
        "percentile": round(100 * sum(value < current for value in values) / len(values), 1) if current is not None else None,
        "industry": benchmark,
        "industry_relation": ("higher" if current > benchmark else "lower" if current < benchmark else "equal")
        if current is not None and benchmark is not None else None,
    }


def valuation_observation_rows(series: dict[str, list[dict]], reports: list[dict]) -> list[dict]:
    """Join each real Baidu observation; PS uses revenue known on that exact day."""
    by_date: dict[str, dict] = {}
    financial = sorted(
        ((row["publish_date"], row["period"], row["revenue_ttm"])
         for row in build_period_rows(reports, [], []) if row.get("publish_date")),
        key=lambda item: (item[0], item[1]),
    )
    disclosures = [item[0] for item in financial]
    for metric in ("pe", "pb", "market_cap"):
        for point in series.get(metric, []):
            day = date.fromisoformat(point["date"]).isoformat()
            row = by_date.setdefault(day, {"date": day, "pe": None, "pb": None, "ps": None})
            if metric in ("pe", "pb"):
                row[metric] = _positive(point.get("value"))
                row[f"{metric}_date"] = day
                if metric == "pe":
                    row["pe_raw"] = _finite_number(point.get("value"))
                continue
            row["ps_date"] = day
            index = bisect_right(disclosures, day) - 1
            if index >= 0:
                published, _, revenue_ttm = financial[index]
                age = (date.fromisoformat(day) - date.fromisoformat(published)).days
                cap = _positive(point.get("value"))
                if cap is not None and revenue_ttm and revenue_ttm > 0 and age <= 400:
                    row["ps"] = _positive(cap * 1e8 / revenue_ttm)
    return [by_date[day] for day in sorted(by_date)]


def aggregate_valuation_rows(rows, frequency, *, as_of=None):
    """Take each metric's last real observation in a Shanghai calendar period."""
    if frequency not in ("day", "week", "month"):
        raise ValueError("Unsupported valuation frequency")
    end = date.fromisoformat(as_of) if as_of else date.today()
    groups: dict[str, list[dict]] = {}
    for row in rows:
        day = date.fromisoformat(row["date"])
        if day > end:
            continue
        if frequency == "day":
            key = day.isoformat()
        elif frequency == "week":
            key = (day - timedelta(days=day.weekday())).isoformat()
        else:
            key = day.strftime("%Y-%m")
        groups.setdefault(key, []).append(row)
    result = []
    for key in sorted(groups):
        items = sorted(groups[key], key=lambda row: row["date"])
        latest = items[-1]["date"]
        if frequency == "day":
            period_end = date.fromisoformat(key)
        elif frequency == "week":
            period_end = date.fromisoformat(key) + timedelta(days=6)
        else:
            month_start = date.fromisoformat(key + "-01")
            period_end = (month_start.replace(year=month_start.year + 1, month=1)
                          if month_start.month == 12 else month_start.replace(month=month_start.month + 1)) - timedelta(days=1)
        aggregated = {"date": latest, "period_complete": period_end < end or frequency != "day" and period_end == end}
        for metric in ("pe", "pb", "ps", "dividend_yield", "qfq_close"):
            selected = next((row for row in reversed(items) if row.get(metric) is not None), None)
            if selected:
                aggregated[metric] = selected[metric]
                aggregated[f"{metric}_date"] = selected.get(f"{metric}_date", selected["date"])
            else:
                aggregated[metric] = None
        result.append(aggregated)
    return result


def valuation_summary_observations(rows, metric, years, industry, *, as_of=None, trading_dates=None):
    if metric not in ("pe", "pb", "ps", "dividend_yield") or years not in (3, 5, 10):
        raise ValueError("不支持的估值指标或时间范围")
    end = date.fromisoformat(as_of) if as_of else date.today()
    try:
        start = end.replace(year=end.year - years)
    except ValueError:
        start = end.replace(year=end.year - years, day=28)
    window = sorted((row for row in rows if start.isoformat() <= row["date"] <= end.isoformat()),
                    key=lambda row: row["date"])
    negative_pe_ranges = []
    if metric == "pe":
        active = None
        for row in window:
            if "pe_raw" not in row:
                continue
            value = row["pe_raw"]
            if value is not None and value < 0:
                if active is None:
                    active = {"start": row["date"], "end": row["date"]}
                    negative_pe_ranges.append(active)
                else:
                    active["end"] = row["date"]
            else:
                active = None
    valid_number = _nonnegative if metric == "dividend_yield" else _positive
    valid = [row for row in window if valid_number(row.get(metric)) is not None]
    known_trade_dates = {day for day in (trading_dates or [])
                         if start.isoformat() <= day <= end.isoformat()}
    display = {frequency: aggregate_valuation_rows(window, frequency, as_of=end.isoformat())
               for frequency in ("day", "week", "month")}
    if known_trade_dates:
        display["day"] = [row for row in display["day"] if row["date"] in known_trade_dates]
    observed_dates = {row["date"] for row in valid}
    dense = bool(known_trade_dates) and known_trade_dates <= observed_dates
    if years == 5:
        weekly_rows = [row for row in valid if not known_trade_dates or row["date"] in known_trade_dates]
        sample = aggregate_valuation_rows(weekly_rows, "week", as_of=end.isoformat())
        sample_frequency = "week"
    elif dense:
        sample = [row for row in valid if row["date"] in known_trade_dates]
        sample_frequency = "trading_day"
    else:
        sample = [row for row in display["month"] if valid_number(row.get(metric)) is not None]
        sample_frequency = "month"
    values = sorted(float(row[metric]) for row in sample)
    latest = valid[-1] if valid else None
    current = valid_number(latest.get(metric)) if latest else None
    benchmark = _positive(industry.get(metric))
    frequency = {3: "day", 5: "week", 10: "month"}[years]
    return {
        "rows": display[frequency], "rows_by_frequency": display, "frequency": frequency,
        "negative_pe_ranges": negative_pe_ranges,
        "count": len(values), "sample_count": len(values),
        "sample_frequency": sample_frequency, "percentile_frequency": sample_frequency,
        "percentile_methodology_version": "uniform_trade_week_or_month_v2",
        "sparse": not dense,
        "sparse_hint": ("较早段观测较稀疏；请以悬浮提示中的实际日期为准。" if not dense else None),
        "current": current,
        "current_date": latest.get(f"{metric}_date", latest["date"]) if latest else None,
        "high": _percentile(values, .8) if values else None,
        "median": _percentile(values, .5) if values else None,
        "low": _percentile(values, .2) if values else None,
        "percentile": round(100 * sum(value < current for value in values) / len(values), 1)
        if current is not None and values else None,
        "industry": benchmark,
        "industry_relation": ("higher" if current > benchmark else "lower" if current < benchmark else "equal")
        if current is not None and benchmark is not None else None,
    }
