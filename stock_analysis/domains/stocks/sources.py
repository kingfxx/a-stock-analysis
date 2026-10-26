"""Sina/Tencent requests and pagination; pure parsing lives in source_parsers.

Parser exports remain available here for existing callers.
"""
from __future__ import annotations

from datetime import date, timedelta
import requests

from stock_analysis.domains.stocks.source_parsers import (
    _number,
    _first,
    _financing_interest_income,
    _date,
    normalize_report_dates,
    parse_financial_reports,
    parse_adjusted_profit_reports,
    parse_cash_flow_reports,
    parse_monthly_prices,
    parse_daily_prices,
    normalize_code,
    symbol_for,
    parse_stock_name,
    parse_price_history,
    price_equal,
    PROFIT_KEYS,
    ADJUSTED_PROFIT_KEYS,
    SHARE_KEYS,
    EQUITY_KEYS,
    TOTAL_EQUITY_KEYS,
    CASH_FLOW_KEYS,
    CAPEX_KEYS,
    DEBT_FIELDS,
    parse_financial_report_page,
    validate_financial_history,
    parse_cash_flow_history,
)


SINA_URL = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"


TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="


def fetch_stock_name(code: str, session: requests.Session) -> str:
    symbol = symbol_for(code)
    response = session.get(TENCENT_QUOTE_URL + symbol, timeout=12)
    response.raise_for_status()
    return parse_stock_name(response.content.decode("gbk", errors="replace"), symbol)


def fetch_financial_report_page(code: str, session: requests.Session, source: str,
                                num: int = 8, page: int = 1) -> dict:
    """Read one raw Sina statement page; ``total`` counts all historical periods."""
    symbol = symbol_for(code)
    if source not in ("lrb", "fzb", "llb", "gjzb") or not isinstance(num, int) or num < 1 or not isinstance(page, int) or page < 1:
        raise ValueError("新浪报表类型或分页参数无效")
    response = session.get(SINA_URL, params={"paperCode": symbol, "source": source,
                                             "type": "0", "page": str(page), "num": str(num)}, timeout=18)
    response.raise_for_status()
    try:
        payload = response.json()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"新浪{source}分页数据不完整或无效") from exc
    return parse_financial_report_page(payload, source, num, page)



def fetch_financial_reports(code: str, session: requests.Session) -> list[dict]:
    symbol = symbol_for(code)

    def one(kind):
        response = session.get(SINA_URL, params={"paperCode": symbol, "source": kind,
                                                 "type": "0", "page": "1", "num": "200"}, timeout=18)
        response.raise_for_status()
        payload = response.json()
        return validate_financial_history(payload, kind)

    return parse_financial_reports(one("lrb"), one("fzb"))


def fetch_cash_flow_reports(code: str, session: requests.Session) -> dict[str, dict]:
    symbol = symbol_for(code)
    response = session.get(SINA_URL, params={"paperCode": symbol, "source": "llb",
                                             "type": "0", "page": "1", "num": "200"}, timeout=18)
    response.raise_for_status()
    payload = response.json()
    return parse_cash_flow_history(payload)


def fetch_monthly_prices(code: str, session: requests.Session, adjust: str = "") -> list[dict]:
    if adjust not in ("", "qfq"):
        raise ValueError("仅支持未复权或前复权月 K")
    symbol = symbol_for(code)
    response = session.get(TENCENT_URL, params={"param": f"{symbol},month,,,640,{adjust}"},
                           headers={"Referer": "https://gu.qq.com/"}, timeout=18)
    response.raise_for_status()
    return parse_monthly_prices(response.json(), symbol, adjust)


def fetch_daily_prices(code: str, session: requests.Session, adjust: str,
                       earliest_date: str, latest_date: str) -> list[dict]:
    """Page backward through Tencent daily K; callers retain only report-date snapshots."""
    if adjust not in ("", "qfq"):
        raise ValueError("仅支持未复权或前复权日 K")
    symbol = symbol_for(code)
    end = date.fromisoformat(latest_date)
    earliest = date.fromisoformat(earliest_date)
    prices = {}
    for _ in range(50):
        if end < earliest:
            break
        response = session.get(TENCENT_URL,
                               params={"param": f"{symbol},day,,{end.isoformat()},640,{adjust}"},
                               headers={"Referer": "https://gu.qq.com/"}, timeout=18)
        response.raise_for_status()
        batch = parse_daily_prices(response.json(), symbol, adjust)
        if not batch:
            break
        prices.update({row["date"]: row for row in batch})
        oldest = min(date.fromisoformat(row["date"]) for row in batch)
        if oldest > end:
            raise ValueError("腾讯日 K 分页没有向历史推进")
        end = oldest - timedelta(days=1)
        if len(batch) < 640:
            break
    else:
        raise ValueError("腾讯日 K 历史分页超过 50 次")
    return [prices[key] for key in sorted(prices)]


def fetch_price_history(code, session, adjust, start="1990-01-01", end=None):
    """Backward pages deliberately overlap 20 trades and validate their basis.

    Return only the requested date range. A short final page is source exhaustion,
    not a claim that every calendar date is a trade or that pre-listing data exists.
    """
    end = end or date.today().isoformat()
    date.fromisoformat(start)
    date.fromisoformat(end)
    if start > end:
        raise ValueError("行情请求范围颠倒")
    symbol = symbol_for(code)
    prices, previous, request_end = {}, None, end
    for _ in range(50):
        remaining_days = (date.fromisoformat(request_end) - date.fromisoformat(start)).days + 1
        page_size = min(640, max(21 if previous is not None else 1, remaining_days))
        response = session.get(TENCENT_URL, params={
            "param": f"{symbol},day,,{request_end},{page_size},{adjust}"},
            headers={"Referer": "https://gu.qq.com/"}, timeout=18)
        response.raise_for_status()
        payload = response.json()
        node = (payload.get("data") or {}).get(symbol, {})
        verified_unadjusted = False
        if adjust == "qfq" and "qfqday" not in node and isinstance(node.get("day"), list):
            # Tencent can return day for an explicitly adjusted request with no
            # adjustment. Never infer this from a raw payload alone: cross-check
            # the same window against both raw and backward-adjusted requests.
            try:
                for basis in ("", "hfq"):
                    check = session.get(TENCENT_URL, params={
                        "param": f"{symbol},day,,{request_end},{page_size},{basis}"},
                        headers={"Referer": "https://gu.qq.com/"}, timeout=18)
                    check.raise_for_status()
                    other = check.json()
                    other_node = (other.get("data") or {}).get(symbol, {})
                    parse_price_history(other, symbol, "")
                    if "hfqday" in other_node or other_node.get("day") != node["day"]:
                        raise ValueError("腾讯前复权 day 返回无法确认复权口径")
                confirm_recovery = getattr(session, "confirm_stock_recovery", None)
                if confirm_recovery:
                    confirm_recovery()
            except Exception as exc:
                abort_recovery = getattr(session, "abort_stock_recovery", None)
                if abort_recovery:
                    abort_recovery(type(exc).__name__)
                raise
            payload = {**payload, "data": {symbol: {"qfqday": node["day"]}}}
            verified_unadjusted = True
        batch = parse_price_history(payload, symbol, adjust)
        if verified_unadjusted:
            for row in batch:
                row["source_basis"] = "tencent-current-ohlc-verified-unadjusted"
        if not batch:
            if previous is not None:
                raise ValueError("腾讯历史分页缺页")
            raise ValueError("腾讯未返回日行情")
        if batch[-1]["date"] > request_end:
            raise ValueError("腾讯忽略了行情结束日期")
        if previous is not None:
            overlap = [row for row in batch if row["date"] in previous]
            if len(overlap) < 20 or any(not price_equal(row, previous[row["date"]]) for row in overlap):
                raise ValueError("腾讯分页重叠不足或复权基准发生变化")
            if batch[0]["date"] >= min(previous):
                raise ValueError("腾讯历史分页未推进")
        prices.update({r["date"]: r for r in batch})
        if batch[0]["date"] <= start or len(batch) < page_size:
            return [prices[d] for d in sorted(prices) if start <= d <= end]
        previous = {r["date"]: r for r in batch}
        request_end = batch[19]["date"]
    raise ValueError("腾讯历史分页超过上限")
