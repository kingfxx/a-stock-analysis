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


def _year_before(period: str) -> str:
    return f"{int(period[:4]) - 1}{period[4:]}"


def _growth(current, previous):
    return (current / previous - 1) * 100 if current is not None and previous is not None and previous > 0 else None


HOVER_YOY_FIELDS = ("revenue", "profit", "operating_cash_flow", "free_cash_flow",
                    "cash_dividend", "capex", "interest_bearing_debt", "net_cash")


def _hover_yoy(current, previous, field: str) -> str:
    """Describe comparable values without misleading percentages on negative bases."""
    if current is None:
        change = "—（本期数据缺失）"
    elif previous is None:
        change = "—（缺少上年同期）"
    elif previous == 0:
        change = "—（上年同期为零）"
    elif current == previous:
        change = "0.00%"
    elif previous < 0:
        if current > 0:
            change = "扭亏为盈" if field == "profit" else "由负转正"
        elif current == 0:
            change = "亏损归零" if field == "profit" else "负值归零"
        elif field == "profit":
            change = "亏损收窄" if current > previous else "亏损扩大"
        else:
            change = "负值收窄" if current > previous else "负值扩大"
    elif current < 0:
        change = "转亏" if field == "profit" else "由正转负"
    else:
        percent = round(_growth(current, previous), 2)
        change = f"{percent:+.2f}%" if percent else "0.00%"
    return "同比 " + change + ("（仅已实施口径）" if field == "cash_dividend" else "")


DEBT_COMPONENTS = ("short_term_borrowings", "short_term_bonds",
                   "current_noncurrent_liabilities", "long_term_borrowings",
                   "bonds_payable", "lease_liabilities")


def _margin(revenue, numerator):
    return numerator / revenue * 100 if revenue is not None and revenue > 0 and numerator is not None else None


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
                      qfq_references: list[dict] | None = None,
                      dividend_events: list[dict] | None = None) -> list[dict]:
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
        cash_quarter = quarter_value("operating_cash_flow_ytd")
        capex_quarter = quarter_value("capex_ytd")
        cash_ytd = report.get("operating_cash_flow_ytd")
        capex_ytd = report.get("capex_ytd")
        debt = (sum(report[field] or 0 for field in DEBT_COMPONENTS)
                if all(field in report for field in DEBT_COMPONENTS) else None)
        cash = report.get("monetary_funds")
        row = {
            **report,
            "revenue_quarter": quarter_value("revenue_ytd"),
            "profit_quarter": quarter_value("profit_ytd"),
            "operating_cost_quarter": quarter_value("operating_cost_ytd"),
            "net_profit_quarter": quarter_value("net_profit_ytd"),
            "operating_cash_flow_quarter": cash_quarter,
            "capex_quarter": capex_quarter,
            "free_cash_flow_quarter": cash_quarter - capex_quarter if cash_quarter is not None and capex_quarter is not None else None,
            "free_cash_flow_ytd": cash_ytd - capex_ytd if cash_ytd is not None and capex_ytd is not None else None,
            "revenue_ttm": None,
            "profit_ttm": None,
            "operating_cost_ttm": None,
            "net_profit_ttm": None,
            "capex_ttm": None,
            "operating_cash_flow_ttm": None,
            "free_cash_flow_ttm": None,
            "interest_bearing_debt": debt,
            "net_cash": cash - debt if cash is not None and debt is not None else None,
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
    dividends_by_period: dict[str, list[dict]] = {}
    for event in dividend_events or []:
        if event.get("report_period") in by_row:
            dividends_by_period.setdefault(event["report_period"], []).append(event)

    def dividend_values(events):
        if dividend_events is None:
            return None, ""
        amounts = [event["per_share"] * event["total_shares"]
                   if event.get("per_share") is not None and event.get("total_shares") is not None
                   else None for event in events]
        details = "、".join(f'{event["report_period"]} → {event["date"]}' for event in events)
        return (sum(amounts) if all(amount is not None for amount in amounts) else None), details

    for row in rows:
        current_period = row["period"]
        for suffix, events in (
            ("quarter", dividends_by_period.get(current_period, [])),
            ("ytd", [event for period, group in dividends_by_period.items()
                     if period[:4] == current_period[:4] and period <= current_period
                     for event in group]),
        ):
            row[f"cash_dividend_{suffix}"], row[f"cash_dividend_details_{suffix}"] = dividend_values(events)
        chain = [row]
        while len(chain) < 4:
            previous = by_row.get(_quarter_before(chain[-1]["period"]))
            if previous is None:
                break
            chain.append(previous)
        if len(chain) == 4:
            ttm_events = [event for item in chain
                          for event in dividends_by_period.get(item["period"], [])]
            row["cash_dividend_ttm"], row["cash_dividend_details_ttm"] = dividend_values(ttm_events)
            for field in ("revenue", "profit", "operating_cost", "net_profit", "capex",
                          "operating_cash_flow", "free_cash_flow"):
                values = [item[f"{field}_quarter"] for item in chain]
                if all(value is not None for value in values):
                    row[f"{field}_ttm"] = sum(values)
        else:
            row["cash_dividend_ttm"] = None
            row["cash_dividend_details_ttm"] = ""
        previous_year = by_row.get(_year_before(row["period"]))
        for suffix in ("quarter", "ytd", "ttm"):
            for field in ("revenue", "profit"):
                row[f"{field}_growth_{suffix}"] = _growth(
                    row.get(f"{field}_{suffix}"),
                    previous_year.get(f"{field}_{suffix}") if previous_year else None)
        equity_now = row.get("equity")
        equity_then = previous_year.get("equity") if previous_year else None
        row["roe"] = (row["profit_ttm"] / ((equity_now + equity_then) / 2) * 100
                      if row["profit_ttm"] is not None and equity_now is not None
                      and equity_then is not None and equity_now > 0 and equity_then > 0 else None)
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
                       "profit": row.get(f"profit_{suffix}"),
                       "cash_dividend": row.get(f"cash_dividend_{suffix}"),
                       "cash_dividend_details": row.get(f"cash_dividend_details_{suffix}"),
                       "capex": row.get(f"capex_{suffix}"),
                       "gross_margin": _margin(row.get(f"revenue_{suffix}"),
                                               (row[f"revenue_{suffix}"] - row[f"operating_cost_{suffix}"])
                                               if row.get(f"revenue_{suffix}") is not None
                                               and row.get(f"operating_cost_{suffix}") is not None else None),
                       "net_margin": _margin(row.get(f"revenue_{suffix}"), row.get(f"net_profit_{suffix}")),
                       "revenue_growth": row.get(f"revenue_growth_{suffix}"),
                       "profit_growth": row.get(f"profit_growth_{suffix}"),
                       "operating_cash_flow": row.get(f"operating_cash_flow_{suffix}"),
                       "free_cash_flow": row.get(f"free_cash_flow_{suffix}")})
    # Compute before the browser applies a date range; never persist derived hover data.
    by_period = {row["period"]: row for row in result}
    for row in result:
        previous = by_period.get(_year_before(row["period"]), {})
        row["yoy"] = {field: _hover_yoy(row.get(field), previous.get(field), field)
                      for field in HOVER_YOY_FIELDS}
    return result
