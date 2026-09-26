"""Read-only BaoStock daily-bar coverage probe for three specified stocks."""

import importlib.metadata
import json
from datetime import datetime, timezone
from pathlib import Path

import baostock as bs

SYMBOLS = ("sh.600519", "sh.600036", "sz.000001")
FIELDS = "date,code,open,high,low,close,volume,amount,adjustflag,tradestatus"
OUTPUT = Path(__file__).with_name("results") / "baostock_probe.json"


def main():
    result = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "baostock_version": importlib.metadata.version("baostock"),
        "query": {"fields": FIELDS, "start_date": "1990-01-01", "end_date": "2026-09-26", "frequency": "d", "adjustflag": "3"},
        "symbols": {},
    }
    login = bs.login()
    result["login"] = {"error_code": login.error_code, "error_msg": login.error_msg}
    if login.error_code == "0":
        try:
            for symbol in SYMBOLS:
                query = bs.query_history_k_data_plus(
                    symbol, FIELDS, start_date="1990-01-01", end_date="2026-09-26",
                    frequency="d", adjustflag="3",
                )
                output = {"error_code": query.error_code, "error_msg": query.error_msg, "fields": query.fields}
                if query.error_code == "0":
                    count, first, last, samples = 0, None, None, []
                    while query.next():
                        row = dict(zip(query.fields, query.get_row_data()))
                        count += 1
                        first = row if first is None else first
                        last = row
                        if row["date"] in {"2024-12-31", "2026-09-24"}:
                            samples.append(row)
                    output.update(row_count=count, first=first, last=last, samples=samples)
                result["symbols"][symbol] = output
        finally:
            bs.logout()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {OUTPUT}")
    print("login", result["login"])
    for symbol, record in result["symbols"].items():
        print(symbol, record.get("error_code"), record.get("row_count"),
              record.get("first", {}).get("date"), record.get("last", {}).get("date"))


if __name__ == "__main__":
    main()
