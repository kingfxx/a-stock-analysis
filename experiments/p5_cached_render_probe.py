"""Measure the read-only cached HTML path in a fresh Python process.

Example (run once for a process-cold result, then compare repeated warm calls):
    python experiments/p5_cached_render_probe.py --database data/stock_analysis.sqlite3 --code 601919

This measures server.render_page, the initial HTML response assembled from local
data. It does not measure OS cold disk I/O, browser rendering, async API calls,
source downloads, or refresh work. Run a new process for each first-call sample.
The probe only reads the database and caches; it does not start the server.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quarterly_dashboard import server  # noqa: E402
from quarterly_dashboard.storage import Database  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True,
                        help="Existing SQLite database or consistent backup; read only")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data",
                        help="Directory containing fundamentals/, valuation/, and chips/")
    parser.add_argument("--code", default="601919")
    parser.add_argument("--warm-runs", type=int, default=10)
    args = parser.parse_args()
    if args.warm_runs < 1:
        parser.error("--warm-runs must be positive")
    database = args.database.resolve(strict=True)
    server.DATABASE_PATH = database
    server.CACHE = args.data_dir / "fundamentals"
    server.VALUATION_CACHE = args.data_dir / "valuation"
    server.CHIP_CACHE = args.data_dir / "chips"

    # Pre-register services so render_page never invokes initialize() on the
    # supplied database. Its normal page path then opens read-only snapshots.
    server.services(initialized=True)

    elapsed_ms = []
    size_bytes = None
    for _ in range(args.warm_runs + 1):
        start = perf_counter_ns()
        html = server.render_page(args.code, refresh=False)
        elapsed_ms.append((perf_counter_ns() - start) / 1_000_000)
        current_size = len(html.encode("utf-8"))
        if size_bytes is not None and current_size != size_bytes:
            raise RuntimeError("Page size changed during the probe; repeat with stable data")
        size_bytes = current_size

    warm = elapsed_ms[1:]
    ordered = sorted(warm)
    check = Database(database).check()
    print(json.dumps({
        "scope": "read-only cached initial HTML; excludes async API and browser rendering",
        "database": str(database),
        "data_dir": str(args.data_dir.resolve()),
        "code": args.code,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "journal_mode": check["journal_mode"],
        "html_bytes": size_bytes,
        "process_first_ms": round(elapsed_ms[0], 3),
        "warm_runs": args.warm_runs,
        "warm_median_ms": round(statistics.median(warm), 3),
        "warm_p95_ms": round(ordered[(95 * len(ordered) - 1) // 100], 3),
        "warm_all_ms": [round(value, 3) for value in warm],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
