"""Targeted follow-up checks for financial fields and Tencent day bars."""

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

OUT = Path(__file__).with_name("results") / "detail_probe.json"
SYMBOLS = {"600519": "sh", "600036": "sh", "000001": "sz"}


def request_json(url, params):
    try:
        r = requests.get(url, params=params, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "utf-8"
        data = r.json()
        return {"status": r.status_code, "data": data}
    except Exception as e:
        return {"error_type": type(e).__name__, "error": str(e)[:300]}


def financial_summary(symbol, prefix, kind):
    x = request_json(
        "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022",
        {"paperCode": f"{prefix}{symbol}", "source": kind, "type": "0", "page": "1", "num": "120"},
    )
    if "data" not in x:
        return x
    data = x["data"].get("result", {}).get("data", {}) or {}
    reports = data.get("report_list", {}) or {}
    summary = {
        "status": x["status"], "report_count": data.get("report_count"),
        "returned_periods": len(reports), "earliest_period": min(reports) if reports else None,
        "latest_period": max(reports) if reports else None,
        "latest_publish_date": reports[max(reports)].get("publish_date") if reports else None,
        "latest_update_time": reports[max(reports)].get("update_time") if reports else None,
        "latest_data_source": reports[max(reports)].get("data_source") if reports else None,
        "latest_fields": {it.get("item_title"): it.get("item_value") for it in reports[max(reports)].get("data", []) if any(t in str(it.get("item_title")) for t in ("营业收入", "归属", "归属于", "所有者权益", "净资产"))} if reports else {},
    }
    return summary


def tencent_day(symbol, prefix, older=False):
    params = f"{prefix}{symbol},day,2001-01-01,2002-12-31,640," if older else f"{prefix}{symbol},day,,,320,"
    x = request_json("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", {"param": params})
    if "data" not in x:
        return x
    payload = x["data"].get("data", {}).get(f"{prefix}{symbol}", {}) or {}
    rows = payload.get("day", [])
    return {"status": x["status"], "code": x["data"].get("code"), "keys": list(payload),
            "row_count": len(rows), "first": rows[0] if rows else None, "last": rows[-1] if rows else None}


def main():
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "symbols": {}}
    for symbol, prefix in SYMBOLS.items():
        result["symbols"][symbol] = {
            "sina_income_120": financial_summary(symbol, prefix, "lrb"),
            "sina_balance_120": financial_summary(symbol, prefix, "fzb"),
            "tencent_unadjusted_320": tencent_day(symbol, prefix),
        }
    result["symbols"]["600519"]["tencent_older_range"] = tencent_day("600519", "sh", older=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {OUT}")
    for code, entries in result["symbols"].items():
        print(code, {k: (v.get("returned_periods") if k.startswith("sina") else v.get("row_count")) for k, v in entries.items()})


if __name__ == "__main__":
    main()
