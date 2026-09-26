"""Bounded, read-only probe for mootdx 0.11.x. Results are evidence, not production data."""

from __future__ import annotations

import importlib.metadata
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from mootdx.quotes import Quotes


SYMBOLS = ("600519", "600036", "000001")
OUTPUT = Path(__file__).with_name("results") / "mootdx_probe.json"


def summarize(value):
    if value is None:
        return {"type": "None", "row_count": 0}
    if hasattr(value, "to_dict") and hasattr(value, "columns"):
        rows = value.to_dict(orient="records")
        columns = list(value.columns)
    elif isinstance(value, dict):
        rows, columns = [value], list(value)
    elif isinstance(value, (list, tuple)):
        rows = list(value)
        columns = list(rows[0]) if rows and isinstance(rows[0], dict) else []
    else:
        return {"type": type(value).__name__, "repr": str(value)[:500]}
    def clean(item):
        if isinstance(item, dict):
            return {str(key): clean(val) for key, val in item.items()}
        if isinstance(item, list):
            return [clean(val) for val in item]
        if isinstance(item, float) and not math.isfinite(item):
            return None
        return item

    output = {
        "type": type(value).__name__,
        "row_count": len(rows),
        "columns": [str(item) for item in columns],
        "first": clean(rows[0]) if rows else None,
        "last": clean(rows[-1]) if rows else None,
    }
    if "category" in columns:
        output["category_counts"] = dict(Counter(str(row.get("category")) for row in rows))
        output["category_examples"] = {str(category): clean(next(row for row in rows if row.get("category") == category))
                                       for category in {row.get("category") for row in rows}}
        output["category_last_examples"] = {str(category): clean(next(row for row in reversed(rows) if row.get("category") == category))
                                            for category in {row.get("category") for row in rows}}
    return output


def attempt(operation):
    try:
        return {"ok": True, **summarize(operation())}
    except Exception as exc:  # probe must continue across independent endpoints
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:500]}


def main():
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "mootdx_version": importlib.metadata.version("mootdx"),
        "symbols": {},
    }
    client = Quotes.factory(market="std", timeout=5, auto_retry=False)
    for symbol in SYMBOLS:
        report["symbols"][symbol] = {
            "daily_recent_1": attempt(lambda s=symbol: client.bars(symbol=s, frequency=9, start=0, offset=10)),
            "daily_recent_2": attempt(lambda s=symbol: client.bars(symbol=s, frequency=9, start=0, offset=10)),
            "daily_older": attempt(lambda s=symbol: client.bars(symbol=s, frequency=9, start=800, offset=10)),
            "finance": attempt(lambda s=symbol: client.finance(symbol=s)),
            "xdxr": attempt(lambda s=symbol: client.xdxr(symbol=s)),
        }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding="utf-8")
    print(f"saved {OUTPUT}")
    for symbol, entries in report["symbols"].items():
        print(symbol, {key: (value.get("row_count") if value["ok"] else value["error_type"]) for key, value in entries.items()})


if __name__ == "__main__":
    main()
