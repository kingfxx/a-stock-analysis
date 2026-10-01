"""Exercise P4 initialization and warm refresh on an isolated copy of the live DB.

Run with ``python -m experiments.p4_integration_probe 601919 600887 000001``.
All files are written under ignored data/verification; the source DB is read only.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from quarterly_dashboard.dividend_service import DividendService
from quarterly_dashboard.fundamental_service import FundamentalService
from quarterly_dashboard.storage import DEFAULT_DATABASE, Database, restore_backup
from quarterly_dashboard.valuation_service import ValuationService


ROOT = Path(__file__).resolve().parent.parent


def main(codes):
    started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "data" / "verification" / "reports" / f"p4-integration-{started}"
    output.mkdir(parents=True)
    candidate = output / "candidate.sqlite3"
    Database(DEFAULT_DATABASE).backup(candidate)
    db = Database(candidate)
    db.initialize()
    calls = []
    original_get = requests.Session.get

    def recording_get(self, url, *args, **kwargs):
        record = {"url": url, "params": kwargs.get("params"), "response_bytes": None}
        calls.append(record)
        response = original_get(self, url, *args, **kwargs)
        record["response_bytes"] = len(response.content)
        record["status_code"] = response.status_code
        return response

    requests.Session.get = recording_get
    results = {}
    try:
        for code in codes:
            before = len(calls)
            fundamental = FundamentalService(db, ROOT / "data" / "fundamentals")
            dividend = DividendService(db, ROOT / "data" / "fundamentals")
            valuation = ValuationService(db, ROOT / "data" / "valuation",
                                         fetch_industry=ValuationService._fetch_industry)
            for importer in (fundamental, dividend, valuation):
                importer.import_legacy(code)
            initial_financial = fundamental.update(code)
            initial_dividend = dividend.update(code)
            initial_valuation = valuation.update(code, initial_financial["reports"])
            midpoint = len(calls)
            warm_financial = fundamental.update(code, refresh=True)
            warm_dividend = dividend.update(code, refresh=True)
            warm_valuation = valuation.update(code, warm_financial["reports"], refresh=True)
            old_path = ROOT / "data" / "valuation" / f"{code}.json"
            old = json.loads(old_path.read_text(encoding="utf-8")) if old_path.exists() else {}
            old_months = {row["date"][:7]: row for row in old.get("rows", [])}
            new_months = {row["date"][:7]: row for row in warm_valuation["rows"]}
            differences = []
            for month in sorted(old_months.keys() & new_months.keys()):
                previous, current = old_months[month], new_months[month]
                if any(previous.get(metric) != current.get(metric) for metric in ("pe", "pb", "ps")):
                    differences.append({"month": month, "old_date": previous["date"],
                                        "new_date": current["date"],
                                        "old": {metric: previous.get(metric) for metric in ("pe", "pb", "ps")},
                                        "new": {metric: current.get(metric) for metric in ("pe", "pb", "ps")}})
            results[code] = {
                "initial": {"financial_reports": len(initial_financial["reports"]),
                            "dividend_events": len(initial_dividend["events"]),
                            "valuation_observations": {metric: data["count"] for metric, data in initial_valuation["coverage"].items()},
                            "warnings": initial_financial["warnings"] + initial_dividend["warnings"] + initial_valuation["warnings"],
                            "requests": calls[before:midpoint]},
                "warm": {"financial_reports": len(warm_financial["reports"]),
                         "dividend_events": len(warm_dividend["events"]),
                         "valuation_observations": {metric: data["count"] for metric, data in warm_valuation["coverage"].items()},
                         "warnings": warm_financial["warnings"] + warm_dividend["warnings"] + warm_valuation["warnings"],
                         "requests": calls[midpoint:]},
                "valuation_monthly_differences": differences,
            }
            print(f"{code}: initial {midpoint - before} requests, warm {len(calls) - midpoint} requests; "
                  f"warnings {len(results[code]['initial']['warnings'])}/{len(results[code]['warm']['warnings'])}", flush=True)
    finally:
        requests.Session.get = original_get
    integrity = db.check()
    backup = db.backup(output / "final-backup.sqlite3")
    restored = restore_backup(backup, output / "restored.sqlite3")
    restored_check = Database(restored).check()
    summary = {"database": str(candidate), "integrity": integrity, "restored_integrity": restored_check,
               "results": results}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output / "summary.json", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:] or ["601919", "600887", "000001"])
