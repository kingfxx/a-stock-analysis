"""Existing cache and projection definitions; versions remain unchanged."""

REPORT_DATE_BASIS = "sina_same_period_shift_v1"
PRICE_REFERENCE_BASIS = "long_trading_gap_v1"
CASH_FLOW_BASIS = "sina_cash_flow_ytd_v1"
FINANCIAL_FIELDS_BASIS = "sina_margin_debt_roic_inputs_v2"
DIVIDEND_BASIS = "eastmoney_implemented_report_period_total_shares_v1"
NEW_REPORT_FIELDS = ("operating_cost_ytd", "net_profit_ytd", "monetary_funds",
                     "short_term_borrowings", "short_term_bonds",
                     "current_noncurrent_liabilities", "long_term_borrowings",
                     "bonds_payable", "lease_liabilities", "total_equity",
                     "profit_before_tax_ytd", "income_tax_expense_ytd",
                     "interest_expense_ytd", "non_operating_interest_income_ytd")
VALUATION_BASIS = "monthly_qfq_overlay_v1"
FINANCIAL_VALUES = ("revenue_ytd", "profit_ytd", "shares", "equity") + NEW_REPORT_FIELDS
CASH_VALUES = ("operating_cash_flow_ytd", "main_business_cash_flow_ytd", "capex_ytd")


def needs_dividends(data: dict) -> bool:
    return data.get("dividend_basis") != DIVIDEND_BASIS or not data.get("dividend_events")
