"""Pure transformations from reported cumulative values to chart-ready quarters."""

from __future__ import annotations

from bisect import bisect_right
from datetime import date


def _quarter_before(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    if month == 3:
        return f"{year - 1}-12-31"
    previous_month = month - 3
    return f"{year}-{previous_month:02d}-{31 if previous_month == 3 else 30}"


def disclosure_snapshots(reports: list[dict], daily_prices: list[dict]) -> list[dict]:
    """Keep only the last trading close at or before each report disclosure."""
    by_date = {price["date"]: price for price in daily_prices}
    dates = sorted(by_date)
    snapshots = []
    for published in sorted({r.get("publish_date") for r in reports if r.get("publish_date")}):
        try:
            disclosure_day = date.fromisoformat(published)
        except ValueError:
            continue
        index = bisect_right(dates, published) - 1
        if index < 0 or (disclosure_day - date.fromisoformat(dates[index])).days > 15:
            continue
        price = by_date[dates[index]]
        snapshots.append({"publish_date": published, "date": price["date"], "close": price["close"]})
    return snapshots


def disclosure_reference_snapshots(reports: list[dict], daily_prices: list[dict]) -> list[dict]:
    """Keep clearly marked prior closes for report dates inside a long trading gap."""
    by_date = {price["date"]: price for price in daily_prices}
    dates = sorted(by_date)
    references = []
    for published in sorted({r.get("publish_date") for r in reports if r.get("publish_date")}):
        try:
            disclosure_day = date.fromisoformat(published)
        except ValueError:
            continue
        index = bisect_right(dates, published) - 1
        if index < 0 or index + 1 >= len(dates):
            continue
        lag = (disclosure_day - date.fromisoformat(dates[index])).days
        if 15 < lag <= 180:
            price = by_date[dates[index]]
            references.append({"publish_date": published, "date": price["date"],
                               "close": price["close"], "lag_days": lag})
    return references


def build_period_rows(reports: list[dict], raw_prices: list[dict], qfq_prices: list[dict],
                      raw_references: list[dict] | None = None,
                      qfq_references: list[dict] | None = None) -> list[dict]:
    by_period = {r["period"]: r for r in reports}
    raw_by_disclosure = {p["publish_date"]: p for p in raw_prices}
    qfq_by_disclosure = {p["publish_date"]: p for p in qfq_prices}
    raw_ref_by_disclosure = {p["publish_date"]: p for p in (raw_references or [])}
    qfq_ref_by_disclosure = {p["publish_date"]: p for p in (qfq_references or [])}
    rows = []
    for period in sorted(by_period):
        report = by_period[period]
        prior = by_period.get(_quarter_before(period))
        is_q1 = period[5:7] == "03"

        def quarter_value(field):
            current = report.get(field)
            if current is None:
                return None
            if is_q1:
                return current
            previous = prior.get(field) if prior else None
            return current - previous if previous is not None else None

        raw = raw_by_disclosure.get(report.get("publish_date"))
        qfq = qfq_by_disclosure.get(report.get("publish_date"))
        raw_ref = raw_ref_by_disclosure.get(report.get("publish_date"))
        qfq_ref = qfq_ref_by_disclosure.get(report.get("publish_date"))
        raw_close = raw.get("close") if raw else None
        qfq_close = qfq.get("close") if qfq else None
        shares = report.get("shares")
        row = {
            **report,
            "revenue_quarter": quarter_value("revenue_ytd"),
            "profit_quarter": quarter_value("profit_ytd"),
            "revenue_ttm": None,
            "profit_ttm": None,
            "raw_price": raw_close,
            "qfq_price": qfq_close,
            "price_date": raw.get("date") if raw else None,
            "qfq_price_date": qfq.get("date") if qfq else None,
            "market_cap": raw_close * shares if raw_close is not None and shares is not None else None,
            "qfq_price_reference": qfq_ref.get("close") if qfq_ref else None,
            "qfq_price_reference_date": qfq_ref.get("date") if qfq_ref else None,
            "qfq_price_reference_lag_days": qfq_ref.get("lag_days") if qfq_ref else None,
            "market_cap_reference": raw_ref["close"] * shares if raw_ref and shares is not None else None,
            "market_cap_reference_date": raw_ref.get("date") if raw_ref else None,
            "market_cap_reference_lag_days": raw_ref.get("lag_days") if raw_ref else None,
        }
        rows.append(row)
    by_row = {r["period"]: r for r in rows}
    for row in rows:
        chain = [row]
        while len(chain) < 4:
            previous = by_row.get(_quarter_before(chain[-1]["period"]))
            if previous is None:
                break
            chain.append(previous)
        if len(chain) == 4:
            for field in ("revenue", "profit"):
                values = [item[f"{field}_quarter"] for item in chain]
                if all(value is not None for value in values):
                    row[f"{field}_ttm"] = sum(values)
    return rows


def view_rows(rows: list[dict], period: str) -> list[dict]:
    if period not in {"quarter", "ttm", "year"}:
        raise ValueError(f"unsupported period: {period}")
    result = []
    for row in rows:
        if period == "year" and row["period"][5:7] != "12":
            continue
        suffix = {"quarter": "quarter", "ttm": "ttm", "year": "ytd"}[period]
        result.append({**row, "revenue": row.get(f"revenue_{suffix}"),
                       "profit": row.get(f"profit_{suffix}")})
    return result
