"""Pure ROIC estimates shared by the dashboard's annual and TTM pipeline.

Amounts must share a currency, unit and consolidated reporting scope. Profit
inputs must cover the same annual or TTM window; balances are its endpoints.
Normalize expense signs at the data-source boundary, not by taking abs() here.
"""

from __future__ import annotations

from math import isfinite


def invested_capital(balance: dict) -> float | None:
    """Total consolidated equity + interest-bearing debt - cash deduction.

    Extension point: `excess_cash` replaces ALL monetary funds when provided.
    Absent key: today's all-cash approximation. Explicit zero: deduct no cash.
    Explicit None: unknown excess cash; return None rather than silently falling
    back to another methodology. Supply this field separately at BOTH endpoints.
    """
    equity = balance.get("total_equity")  # Includes non-controlling interests.
    debt = balance.get("interest_bearing_debt")
    excess_cash = balance.get("excess_cash", balance.get("monetary_funds"))
    if any(value is None or not isfinite(value) for value in (equity, debt, excess_cash)):
        return None
    if debt < 0 or excess_cash < 0:
        return None
    return equity + debt - excess_cash


def calculate_roic(profit: dict, opening_balance: dict, closing_balance: dict) -> float | None:
    """Return a percentage, or None for missing/invalid calculation inputs.

    NOPAT = (PBT + interest expense - non-operating interest income
             - non_operating_adjustments) * (1 - income tax expense / PBT).
    ROIC = NOPAT / average opening and closing invested capital * 100.

    Extension point: `non_operating_adjustments` is a signed PRE-TAX net amount
    for the SAME profit window. Positive removes gains; negative adds back losses.
    It EXCLUDES non-operating interest income, already deducted separately above,
    to prevent double counting. Absent key means today's no-adjustment default 0;
    explicit None means unknown adjustment, so the estimate is unavailable.
    Future removals must match the non-operating assets excluded from capital.

    This is a simplified estimate, not a fully adjusted operating ROIC. Keep
    remaining financial holdings and their income treatment consistent. Do not
    average quarterly tax rates or automatically substitute a 25% tax rate.
    """
    pbt = profit.get("profit_before_tax")
    tax = profit.get("income_tax_expense")
    interest = profit.get("interest_expense")  # Non-negative expense magnitude.
    interest_income = profit.get("non_operating_interest_income")
    non_operating_adjustments = profit.get("non_operating_adjustments", 0)
    values = (pbt, tax, interest, interest_income, non_operating_adjustments)
    if any(value is None or not isfinite(value) for value in values):
        return None
    if pbt <= 0 or not 0 <= tax <= pbt or interest < 0 or interest_income < 0:
        return None
    opening = invested_capital(opening_balance)
    closing = invested_capital(closing_balance)
    if opening is None or closing is None:
        return None
    average_capital = (opening + closing) / 2
    if average_capital <= 0:
        return None
    nopat = (pbt + interest - interest_income - non_operating_adjustments) * (1 - tax / pbt)
    return nopat / average_capital * 100
