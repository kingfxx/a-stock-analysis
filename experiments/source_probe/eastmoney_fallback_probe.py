"""Small fallback check for historical amount; not a proposed primary source."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SYMBOLS = {"600519": 1, "600036": 1, "000001": 0}
OUTPUT = Path(__file__).with_name("results") / "eastmoney_fallback_probe.json"


def main():
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "symbols": {}}
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
    for symbol, market in SYMBOLS.items():
        try:
            response = session.get(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                params={"secid": f"{market}.{symbol}", "fields1": "f1,f2,f3,f4,f5,f6",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                        "klt": "101", "fqt": "0", "beg": "19900101", "end": "20500101"},
                timeout=15,
            )
            data = (response.json().get("data") or {}) if response.ok else {}
            rows = data.get("klines") or []
            result["symbols"][symbol] = {"status": response.status_code, "count": len(rows),
                                         "first": rows[0] if rows else None,
                                         "last": rows[-1] if rows else None}
        except Exception as exc:
            result["symbols"][symbol] = {"error_type": type(exc).__name__, "error": str(exc)[:300]}
        time.sleep(1.5)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {OUTPUT}")
    for symbol, record in result["symbols"].items():
        print(symbol, record.get("status", record.get("error_type")), record.get("count"))


if __name__ == "__main__":
    main()
