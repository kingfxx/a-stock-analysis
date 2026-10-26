"""Financial view assembly from saved facts."""
from __future__ import annotations

from stock_analysis.domains.financials.core import build_period_rows, view_rows
from stock_analysis.domains.stocks.source_parsers import normalize_report_dates
from stock_analysis.domains.stocks import data_contracts as contracts


def _financial_payload(data: dict) -> dict:
    if not data:
        return {"views": {}, "warnings": []}
    reports = data.get("reports", [])
    if data.get("report_date_basis") != contracts.REPORT_DATE_BASIS:
        reports = normalize_report_dates(reports)
    prices = (data.get("prices", {}) if data.get("price_basis") == "disclosure"
              and data.get("report_date_basis") == contracts.REPORT_DATE_BASIS else {})
    rows = build_period_rows(reports, prices.get("raw", []), prices.get("qfq", []),
                             prices.get("raw_reference"), prices.get("qfq_reference"),
                             data.get("dividend_events") or None,
                             period_end_prices=prices.get("raw_period_end"))
    return {"code": data["code"], "name": data.get("name"), "updated_at": data.get("updated_at"),
            "views": {name: view_rows(rows, name) for name in ("quarter", "ytd", "year", "ttm")},
            "warnings": data.get("warnings", []), "needs_dividends": contracts.needs_dividends(data),
            "announcement_sync": data.get("announcement_sync", {}),
            "business_segment_sync": data.get("business_segment_sync", {})}
