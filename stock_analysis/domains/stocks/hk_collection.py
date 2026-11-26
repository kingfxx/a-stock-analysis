"""Dispatch the HK portion of the existing individual-stock operation."""

from stock_analysis.domains.financials.hk_segments import collect as collect_segments
from stock_analysis.domains.financials.hk_statement_service import collect as collect_statements
from stock_analysis.domains.stocks.hk_announcements import collect as collect_announcements
from stock_analysis.domains.stocks.hk_dividends import collect as collect_dividends
from stock_analysis.domains.valuation.hk_valuation import collect as collect_valuation


def collect(db, collector, code, dataset, full):
    handler = {'financial': collect_statements, 'business_segments': collect_segments,
               'announcements': collect_announcements, 'valuation': collect_valuation,
               'dividends': collect_dividends}[dataset]
    handler(db, collector, code, full)
