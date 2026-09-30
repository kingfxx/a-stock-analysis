"""Shareholder observations and daily margin-financing history from EastMoney."""

from __future__ import annotations

import math
from bisect import bisect_right
from datetime import date, timedelta

from .sources import normalize_code


DATA_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
CHIP_BASIS = "eastmoney_f10_raw_history_v2"
REPORTS = {
    "shareholders": ("RPT_F10_EH_HOLDERNUM", "SECURITY_CODE", "END_DATE"),
    "financing": ("RPTA_WEB_RZRQ_GGMX", "SCODE", "DATE"),
}
VALUE_FIELDS = {
    "shareholders": ("holders", "close", "announced_on"),
    "financing": ("margin_balance", "total_balance", "net_buy", "close"),
}


def _number(value, minimum=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        return None
    return result


def _date(value):
    result = str(value or "")[:10]
    date.fromisoformat(result)
    return result


def _fetch_chip_report(code, section, session, report_override=None):
    """Read every page; reject incomplete or changing pagination before caching."""
    report, code_field, date_field = REPORTS[section]
    report = report_override or report
    code = normalize_code(code)
    records, expected_count, expected_pages = [], None, None
    for page in range(1, 101):
        response = session.get(DATA_URL, params={
            "reportName": report, "columns": "ALL", "filter": f'({code_field}="{code}")',
            "pageNumber": page, "pageSize": 500, "sortTypes": "-1",
            "sortColumns": date_field, "source": "WEB", "client": "WEB",
        }, headers={"Referer": "https://data.eastmoney.com/"}, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if page == 1 and payload.get("code") == 9201 and payload.get("result") is None:
            return []  # Explicit no-record response, not a transport failure.
        if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
            raise ValueError(f"东方财富筹码数据请求失败：{payload.get('message', '响应不完整')}")
        result = payload["result"]
        count, pages = int(result["count"]), int(result["pages"])
        batch = result["data"]
        if count < 0 or pages < 1 or pages > 100 or not isinstance(batch, list):
            raise ValueError("东方财富筹码分页信息异常")
        if expected_count is None:
            expected_count, expected_pages = count, pages
        if (count, pages) != (expected_count, expected_pages) or (count and not batch):
            raise ValueError("东方财富筹码分页发生变化或缺页，请重试")
        if any(not isinstance(row, dict) or row.get(code_field) != code for row in batch):
            raise ValueError("东方财富筹码数据股票代码不匹配")
        records.extend(batch)
        if page == pages:
            if len(records) != count:
                raise ValueError(f"东方财富筹码历史不完整：{len(records)}/{count} 条")
            # Date uniqueness and required values are checked before returning.
            chip_rows(records, section)
            return sorted(records, key=lambda row: row[date_field])
    raise ValueError("东方财富筹码分页超过上限")


def fetch_chip_records(code, section, session):
    records = _fetch_chip_report(code, section, session)
    if section == "shareholders":
        # F10 covers A/H issuers and older history; the detail table sometimes
        # has additional interim observations. Prefer F10 on matching dates.
        details = _fetch_chip_report(code, section, session, "RPT_HOLDERNUM_DET")
        by_date = {row["END_DATE"][:10]: row for row in details}
        by_date.update({row["END_DATE"][:10]: row for row in records})
        records = [by_date[day] for day in sorted(by_date)]
    return records


def chip_rows(records, section, prices=None):
    """Derive chart fields; retain original units and all raw fields on disk."""
    output = {}
    snapshots = {row["date"]: row for row in prices or []}
    for record in records:
        if section == "shareholders":
            day = _date(record.get("END_DATE"))
            holders = _number(record.get("HOLDER_TOTAL_NUM", record.get("HOLDER_NUM")), 1)
            if holders is None or not holders.is_integer():
                raise ValueError("股东人数缺失或格式不正确")
            notice = record.get("NOTICE_DATE", record.get("HOLD_NOTICE_DATE"))
            snapshot = snapshots.get(day, {})
            row = {"date": day, "holders": int(holders),
                   "announced_on": _date(notice) if notice else None,
                   "close": snapshot.get("close") if prices is not None else _number(record.get("CLOSE_PRICE"), 0.000001),
                   "price_date": snapshot.get("price_date")}
        else:
            day = _date(record.get("DATE"))
            balance = _number(record.get("RZYE"), 0)
            if balance is None:
                raise ValueError("融资余额缺失或格式不正确")
            row = {"date": day, "margin_balance": balance,
                   "total_balance": _number(record.get("RZRQYE"), 0),
                   "net_buy": _number(record.get("RZJME")),
                   "close": _number(record.get("SPJ"), 0.000001)}
        if day in output:
            raise ValueError(f"筹码历史存在重复日期 {day}，请重试")
        output[day] = row
    return [output[day] for day in sorted(output)]


def shareholder_price_snapshots(records, prices):
    """Match each observation to a prior close within 15 days, never a future trade."""
    prices = sorted(prices, key=lambda row: row["date"])
    days = [row["date"] for row in prices]
    snapshots = []
    for row in chip_rows(records, "shareholders"):
        day = row["date"]
        index = bisect_right(days, day) - 1
        if index < 0:
            continue
        price = prices[index]
        delay = (date.fromisoformat(day) - date.fromisoformat(price["date"])).days
        close = _number(price.get("close"), 0.000001)
        if delay <= 15 and close is not None:
            snapshots.append({"date": day, "price_date": price["date"], "close": close})
    return snapshots


def missing_chip_history(old, fresh, section):
    """Never replace known dates or valid chart fields with incomplete results."""
    by_date = {row["date"]: row for row in fresh}
    return [row["date"] for row in old if row["date"] not in by_date
            or any(row.get(field) is not None and by_date[row["date"]].get(field) is None
                   for field in VALUE_FIELDS[section])]


def chip_payload(data, section, today=None):
    today = today or date.today()
    rows = chip_rows(data.get("records", []), section, data.get("prices"))
    latest = next((row for row in reversed(rows) if row["date"] <= today.isoformat()), None)
    stored_count = len(rows)
    start = (today - timedelta(days=365)).isoformat()
    if section == "financing":
        rows = [row for row in rows if start <= row["date"] <= today.isoformat()]
    return {"rows": rows, "latest": latest, "stored_count": stored_count,
            "updated_on": data.get("updated_on"), "warnings": data.get("warnings", []),
            "window_start": start if section == "financing" else None,
            "window_end": today.isoformat() if section == "financing" else None,
            "empty": data.get("empty", False)}
