"""Valuation view assembly from saved observations and prices."""
from __future__ import annotations

from datetime import date, timedelta
from stock_analysis.domains.stocks.price_service import month_closes
from stock_analysis.domains.valuation.valuation import merge_adjusted_prices, merge_dividend_yields, valuation_summary, aggregate_valuation_rows, cash_dividend_per_share_365, daily_dividend_yield_observations, monthly_dividend_yields, valuation_summary_observations


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
