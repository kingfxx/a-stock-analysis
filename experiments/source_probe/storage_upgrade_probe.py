"""Read-only P0 checks; save public source samples, never modify dashboard caches.

Run from the repository root: python -m experiments.source_probe.storage_upgrade_probe
This is a reproducible observation, not a guarantee about future source behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from quarterly_dashboard.chips import DATA_URL, _fetch_chip_report
from quarterly_dashboard.network import create_data_session
from quarterly_dashboard.sources import TENCENT_URL, parse_daily_prices, symbol_for

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "docs" / "data-sources" / "storage-upgrade-p0-samples.json"


class RecordingSession:
    def __init__(self, session):
        self.session = session
        self.calls = []

    def get(self, url, **kwargs):
        response = self.session.get(url, **kwargs)
        self.calls.append({"url": url, "params": kwargs.get("params"),
                           "status": response.status_code})
        return response


def financing_window(code, session, start, end):
    response = session.get(DATA_URL, params={
        "reportName": "RPTA_WEB_RZRQ_GGMX", "columns": "ALL",
        "filter": f'(SCODE="{code}")(DATE>=\'{start}\')(DATE<=\'{end}\')',
        "pageNumber": 1, "pageSize": 500, "sortTypes": "-1", "sortColumns": "DATE",
        "source": "WEB", "client": "WEB",
    }, headers={"Referer": "https://data.eastmoney.com/"}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
        raise ValueError("Financing window response is incomplete")
    result = payload["result"]
    rows = result.get("data") or []
    if result["pages"] != 1 or len(rows) != result["count"]:
        raise ValueError("Probe window exceeds a single page or is incomplete")
    if any(row.get("SCODE") != code or not start <= row["DATE"][:10] <= end for row in rows):
        raise ValueError("Date filter was ignored or stock mismatched")
    return rows


def price_page(code, session, end, adjustment="qfq"):
    symbol = symbol_for(code)
    response = session.get(TENCENT_URL, params={
        "param": f"{symbol},day,,{end},640,{adjustment}"},
        headers={"Referer": "https://gu.qq.com/"}, timeout=18)
    response.raise_for_status()
    payload = response.json()
    parsed = parse_daily_prices(payload, symbol, adjustment)
    if not parsed or len({r["date"] for r in parsed}) != len(parsed):
        raise ValueError("Missing prices or duplicate dates")
    raw = payload["data"][symbol][f"{adjustment}day"]
    return parsed, raw


def inspect_stock(code, session, as_of):
    full = _fetch_chip_report(code, "financing", session)
    latest = full[-1]["DATE"][:10] if full else as_of.isoformat()
    start = (date.fromisoformat(latest) - timedelta(days=30)).isoformat()
    window = financing_window(code, session, start, latest)
    expected = {r["DATE"][:10]: r for r in full if start <= r["DATE"][:10] <= latest}
    received = {r["DATE"][:10]: r for r in window}
    missing = sorted(set(expected) - set(received))
    changed = sorted(day for day in expected.keys() & received.keys()
                     if any(expected[day].get(k) != received[day].get(k)
                            for k in ("RZYE", "RZRQYE", "RZJME", "SPJ")))
    source_holders = {}
    for report in ("RPT_F10_EH_HOLDERNUM", "RPT_HOLDERNUM_DET"):
        rows = _fetch_chip_report(code, "shareholders", session, report)
        source_holders[report] = {
            "count": len(rows), "first": rows[0] if rows else None,
            "latest": rows[-1] if rows else None,
            "scope": "unverified; preserve source identity, do not infer A-share/total equivalence",
        }
    current, current_raw = price_page(code, session, as_of.isoformat())
    # Deliberate overlap, unlike production's currently non-overlapping pagination.
    previous_end = current[-200]["date"] if len(current) >= 200 else current[0]["date"]
    older, older_raw = price_page(code, session, previous_end)
    current_map = {r["date"]: r for r in current}
    overlap = [r for r in older if r["date"] in current_map]
    mismatches = [{"date": r["date"], "current": current_map[r["date"]]["close"],
                   "older_end": r["close"]} for r in overlap
                  if Decimal(str(r["close"])) != Decimal(str(current_map[r["date"]]["close"]))]
    result = {
        "financing": {"full_count": len(full), "first_date": full[0]["DATE"][:10] if full else None,
                       "last_date": latest, "window_start": start, "window_count": len(window),
                       "missing_known_dates": missing, "changed_core_dates_during_probe": changed,
                       "sample": full[-1] if full else None,
                       "all_source_fields": sorted(set().union(*(r.keys() for r in full)))},
        "shareholders": source_holders,
        "qfq_overlap": {"current_end": as_of.isoformat(), "older_end": previous_end,
                        "current_count": len(current), "older_count": len(older),
                        "overlap_count": len(overlap), "mismatch_count": len(mismatches),
                        "mismatch_examples": mismatches[:5],
                        "current_first": current_raw[0], "current_last": current_raw[-1],
                        "older_first": older_raw[0], "older_last": older_raw[-1],
                        "consistent_in_this_sample": bool(overlap) and not mismatches,
                        "covers_full_history": False},
    }
    return result


def cache_inventory():
    output = []
    for folder in ("fundamentals", "valuation", "chips"):
        for path in sorted((ROOT / "data" / folder).rglob("*.json")):
            contents = path.read_bytes()
            entry = {"path": path.relative_to(ROOT).as_posix(), "bytes": len(contents),
                     "sha256": hashlib.sha256(contents).hexdigest()}
            try:
                data = json.loads(contents)
                entry["basis"] = data.get("basis")
                entry["counts"] = {key: len(data[key]) for key in (
                    "records", "reports", "rows", "dividend_events") if isinstance(data.get(key), list)}
            except (ValueError, AttributeError) as exc:
                entry["error"] = str(exc)
            output.append(entry)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", nargs="+", default=["601919", "600887", "000001"])
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = {"observed_at_utc": datetime.now(timezone.utc).isoformat(),
              "as_of": args.as_of.isoformat(), "python": platform.python_version(),
              "sqlite": sqlite3.sqlite_version, "baseline": cache_inventory(), "stocks": {}}
    with create_data_session() as base:
        session = RecordingSession(base)
        for code in args.codes:
            try:
                result["stocks"][code] = inspect_stock(code, session, args.as_of)
                stock = result["stocks"][code]
                print(code, "financing", stock["financing"]["full_count"],
                      "window", stock["financing"]["window_count"],
                      "qfq mismatches", stock["qfq_overlap"]["mismatch_count"], flush=True)
            except Exception as exc:
                result["stocks"][code] = {"error": f"{type(exc).__name__}: {exc}"}
                print(code, result["stocks"][code]["error"], flush=True)
        result["requests"] = session.calls
    if cache_inventory() != result["baseline"]:
        raise RuntimeError("Cache inventory changed during the read-only probe")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved", args.output, flush=True)


if __name__ == "__main__":
    main()
