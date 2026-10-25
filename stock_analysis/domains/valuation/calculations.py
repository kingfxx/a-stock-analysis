"""Pure valuation and dividend calculations; no source requests or storage."""
from __future__ import annotations

import math
from statistics import fmean, pstdev
from bisect import bisect_right
from datetime import date, timedelta
from stock_analysis.domains.financials.core import build_period_rows

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


def cash_dividend_per_share_365(events: list[dict], observation_date: str) -> float:
    """Sum implemented pre-tax cash dividends per share in (date - 365 days, date]."""
    end = date.fromisoformat(observation_date)
    start = end - timedelta(days=365)
    return sum(float(event["per_share"]) for event in events
               if event.get("date") and start < date.fromisoformat(event["date"]) <= end
               and event.get("per_share") is not None)


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
        cash = cash_dividend_per_share_365(events, end.isoformat())
        result.append({"date": price["date"], "value": cash / close * 100, "raw_close": close,
                       "cash_dividend_per_share_365": cash})
    return result


def daily_dividend_yield_observations(raw_prices: list[dict], events: list[dict]) -> list[dict]:
    """Build daily trailing yields from saved raw closes and paid dividend events."""
    paid = sorted((date.fromisoformat(event["date"]), float(event["per_share"]))
                  for event in events if event.get("date") and event.get("per_share") is not None)
    result = []
    for price in sorted(raw_prices, key=lambda row: row["date"]):
        close = _positive(price.get("close"))
        if close is None:
            continue
        day = date.fromisoformat(price["date"])
        start = day - timedelta(days=365)
        cash = sum(amount for ex_date, amount in paid if start < ex_date <= day)
        result.append({"date": day.isoformat(), "dividend_yield": cash / close * 100, "raw_close": close,
                       "cash_dividend_per_share_365": cash})
    return result


def merge_dividend_yields(rows: list[dict], yields: list[dict]) -> list[dict]:
    by_month = {row["date"][:7]: dict(row) for row in rows}
    for point in yields:
        month = point["date"][:7]
        row = by_month.setdefault(month, {"date": point["date"], "pe": None,
                                          "pb": None, "ps": None})
        row["date"] = max(row["date"], point["date"])
        row["dividend_yield"] = point["value"]
        row["dividend_yield_date"] = point["date"]
        row["raw_close"] = point.get("raw_close")
        row["cash_dividend_per_share_365"] = point.get("cash_dividend_per_share_365")
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


def _standard_deviation_benchmark(values):
    """Describe the selected historical population; keep negative lower bounds."""
    mean = fmean(values) if values else None
    stddev = pstdev(values, mu=mean) if values else None
    return {"mean": mean, "stddev": stddev,
            "stddev_high": mean + stddev if values else None,
            "stddev_low": mean - stddev if values else None}


def valuation_summary(rows: list[dict], metric: str, years: int, industry: dict,
                      as_of: str | None = None, *, sample_rows: list[dict] | None = None) -> dict:
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
    sample_window = window if sample_rows is None else sorted(
        (row for row in sample_rows if start.isoformat() <= row["date"] <= end.isoformat()),
        key=lambda row: row["date"])
    samples = [row for row in sample_window if valid_number(row.get(metric)) is not None]
    values = sorted(float(row[metric]) for row in samples)
    latest = valid[-1] if valid else None
    current = valid_number(latest.get(metric)) if latest else None
    benchmark = _positive(industry.get(metric))
    return {
        "rows": window, "count": len(values), "current": current,
        "current_date": (latest.get(f"{metric}_date") or latest["date"]) if latest else None,
        **_standard_deviation_benchmark(values),
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
        for metric in ("pe", "pb", "ps", "dividend_yield", "qfq_close", "raw_close",
                       "cash_dividend_per_share_365"):
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
    values = sorted(float(row[metric]) for row in valid)
    latest = valid[-1] if valid else None
    current = valid_number(latest.get(metric)) if latest else None
    benchmark = _positive(industry.get(metric))
    frequency = {3: "day", 5: "week", 10: "month"}[years]
    return {
        "rows": display[frequency], "rows_by_frequency": display, "frequency": frequency,
        "negative_pe_ranges": negative_pe_ranges,
        "count": len(values), "sample_count": len(values),
        "sample_frequency": "day", "percentile_frequency": "day",
        "percentile_methodology_version": "uniform_daily_observations_v1",
        "sparse": not dense,
        "sparse_hint": ("较早段观测较稀疏；请以悬浮提示中的实际日期为准。" if not dense else None),
        "current": current,
        "current_date": latest.get(f"{metric}_date", latest["date"]) if latest else None,
        **_standard_deviation_benchmark(values),
        "high": _percentile(values, .8) if values else None,
        "median": _percentile(values, .5) if values else None,
        "low": _percentile(values, .2) if values else None,
        "percentile": round(100 * sum(value < current for value in values) / len(values), 1)
        if current is not None and values else None,
        "industry": benchmark,
        "industry_relation": ("higher" if current > benchmark else "lower" if current < benchmark else "equal")
        if current is not None and benchmark is not None else None,
    }

