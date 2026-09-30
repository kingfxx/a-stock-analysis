"""Pure consumer projections from one price generation, without source requests."""

from bisect import bisect_right
from datetime import date

from .core import disclosure_reference_snapshots, disclosure_snapshots
from .price_service import month_closes
from .valuation import merge_adjusted_prices


def financial_prices(data, raw, qfq):
    if not data:
        return {}
    prices = dict(data.get("prices", {})) if data.get("price_basis") == "disclosure" else {}
    reports = data.get("reports", [])
    if raw:
        prices["raw"] = disclosure_snapshots(reports, raw)
        prices["raw_reference"] = disclosure_reference_snapshots(reports, raw)
    prices["qfq"] = disclosure_snapshots(reports, qfq)
    prices["qfq_reference"] = disclosure_reference_snapshots(reports, qfq)
    return {**data, "prices": prices, "price_basis": "disclosure"}


def valuation_prices(data, qfq):
    rows = [{k: v for k, v in row.items() if k not in {"qfq_close", "qfq_close_date"}}
            for row in data.get("rows", [])]
    return {**data, "rows": merge_adjusted_prices(rows, month_closes(qfq))}


def chip_prices(data, section, raw, qfq, version):
    raw_by_day, qfq_by_day = ({r["date"]: r for r in prices} for prices in (raw, qfq))
    raw_days, qfq_days = sorted(raw_by_day), sorted(qfq_by_day)
    def match(rows):
        for row in rows:
            day = row["date"]
            for adjustment, by_day, days in (("raw", raw_by_day, raw_days), ("qfq", qfq_by_day, qfq_days)):
                if section == "financing":
                    price = by_day.get(day)
                else:
                    index = bisect_right(days, day) - 1
                    price = by_day[days[index]] if index >= 0 else None
                    if price and (date.fromisoformat(day) - date.fromisoformat(price["date"])).days > 15:
                        price = None
                row[adjustment + "_close"] = price["close"] if price else row.get("raw_close") if adjustment == "raw" and section == "financing" else None
                row[adjustment + "_price_date"] = price["date"] if price else day if adjustment == "raw" and row.get("raw_close") is not None else None
            row["close"], row["price_date"] = row["qfq_close"], row["qfq_price_date"]
        return rows
    result = {**data, "rows": match(data.get("rows", [])), "price_version": version, "price_adjustment": "qfq"}
    for series in result.get("series", []):
        match(series["rows"])
    return result
