"""SQLite foundations; no source requests or dashboard business migration here.

Connections belong to their calling thread and close at context exit. Source work
and validation happen before complete_sync's short atomic write transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from uuid import uuid4
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

DEFAULT_DATABASE = Path(__file__).resolve().parent.parent / "data" / "stock_analysis.sqlite3"
APPLICATION_ID = 0x4153544B  # ASTK; refuse an unrelated SQLite file.
MIGRATIONS = ((1, "001_initial.sql"), (2, "002_shared_data.sql"), (3, "003_p4_facts.sql"))


class StorageError(RuntimeError):
    pass


class StaleSyncError(StorageError):
    """Another run committed after this run read its base revision."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def wal_is_supported(version: tuple[int, ...]) -> bool:
    """Versions containing SQLite's documented WAL-reset fix, not just WAL support."""
    return (version >= (3, 51, 3) or (3, 50, 7) <= version < (3, 51, 0)
            or (3, 44, 6) <= version < (3, 45, 0))


def choose_journal_mode(requested: str, version: tuple[int, ...]) -> str:
    if requested not in {"auto", "delete", "wal"}:
        raise ValueError("journal_mode must be auto, delete or wal")
    if requested == "wal" and not wal_is_supported(version):
        raise StorageError(f"SQLite {'.'.join(map(str, version))} lacks the known WAL-reset fix; use delete")
    return "wal" if requested == "wal" or requested == "auto" and wal_is_supported(version) else "delete"


def _sql_statements(text: str) -> Iterator[str]:
    """Unlike executescript, preserve the caller's transaction including DDL."""
    statement = ""
    for line in text.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        raise StorageError("Incomplete migration SQL")


def _migration_files():
    directory = Path(__file__).with_name("migrations")
    for version, name in MIGRATIONS:
        # Universal newlines make checksums portable across Git's LF/CRLF checkouts.
        sql = (directory / name).read_text(encoding="utf-8")
        yield version, name, hashlib.sha256(sql.encode("utf-8")).hexdigest(), sql


def _day(value: str | None):
    if value is not None and date.fromisoformat(value).isoformat() != value:
        raise ValueError("Dates must be YYYY-MM-DD")


def _raw_payload(value) -> tuple[str, str]:
    if isinstance(value, str):
        parsed = json.loads(value)
    else:
        parsed = value
    encoded = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SyncKey:
    instrument_id: int
    dataset: str
    source: str
    adjustment: str = "raw"

    def values(self):
        if not self.dataset or not self.source or self.adjustment not in {"raw", "qfq"}:
            raise ValueError("Invalid synchronization key")
        return self.instrument_id, self.dataset, self.source, self.adjustment


@dataclass(frozen=True)
class SyncResult:
    record_count: int
    coverage_start: str | None = None
    coverage_end: str | None = None
    data_watermark: str | None = None
    no_data: bool = False
    active_price_version_id: int | None = None
    next_full_audit_at: str | None = None
    checked_at: str | None = None  # Legacy import preserves its original source check time.

    def validate(self):
        if type(self.record_count) is not int or self.record_count < 0:
            raise ValueError("record_count must be a nonnegative integer")
        for day in (self.coverage_start, self.coverage_end, self.data_watermark):
            _day(day)
        if (self.coverage_start is None) != (self.coverage_end is None):
            raise ValueError("Both coverage bounds must be provided together")
        if self.coverage_start is not None and self.coverage_start > self.coverage_end:
            raise ValueError("Inverted coverage range")
        if self.data_watermark is not None and (self.coverage_end is None
                or not self.coverage_start <= self.data_watermark <= self.coverage_end):
            raise ValueError("Watermark must lie within coverage")
        if self.no_data and (self.record_count or self.data_watermark or self.active_price_version_id):
            raise ValueError("No-data result cannot contain records, a watermark or a price version")
        if not self.no_data and (self.coverage_start is None or self.data_watermark is None):
            raise ValueError("Successful data result requires coverage and a data watermark")
        for value in (self.next_full_audit_at, self.checked_at):
            if value is None:
                continue
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if timestamp.utcoffset() != timedelta(0):
                raise ValueError("Audit timestamps must use UTC")


class Database:
    def __init__(self, path: Path | str = DEFAULT_DATABASE, *, journal_mode="auto", busy_timeout_ms=5000):
        self.path = Path(path).resolve()
        self.journal_mode = choose_journal_mode(journal_mode, sqlite3.sqlite_version_info)
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.busy_timeout_ms = busy_timeout_ms

    def _open(self, mode="rw"):
        conn = sqlite3.connect(f"{self.path.as_uri()}?mode={mode}", uri=True,
                               timeout=self.busy_timeout_ms / 1000, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            conn.execute("PRAGMA synchronous=FULL")
            if mode == "ro":
                conn.execute("PRAGMA query_only=ON")
            return conn
        except BaseException:
            conn.close()
            raise

    def _validate_schema(self, conn, *, current=True):
        if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise StorageError("Not an A-stock-analysis database")
        try:
            history = conn.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version").fetchall()
        except sqlite3.DatabaseError as exc:
            raise StorageError("Missing migration history") from exc
        expected = [(v, n, h) for v, n, h, _ in _migration_files()]
        actual = [tuple(row) for row in history]
        if actual != expected[:len(actual)] or len(actual) > len(expected):
            raise StorageError("Unknown or changed database migration; use the matching application version")
        if conn.execute("PRAGMA user_version").fetchone()[0] != (actual[-1][0] if actual else 0):
            raise StorageError("Schema version and migration history disagree")
        if current and len(actual) != len(expected):
            raise StorageError("Database needs initialization/migration")
        return len(actual)

    def initialize(self):
        """Call under instance_lock before starting workers or a schema migration."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._open("rwc")
        try:
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if tables or app_id:
                applied = self._validate_schema(conn, current=False)
            else:
                applied = 0
            migrations = list(_migration_files())
            if applied and applied < len(migrations):
                self.backup(self.path.parent / "backups" /
                            f"pre-migration-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.sqlite3")
            actual_mode = conn.execute(f"PRAGMA journal_mode={self.journal_mode}").fetchone()[0]
            if actual_mode != self.journal_mode:
                raise StorageError(f"Could not select journal mode {self.journal_mode}: {actual_mode}")
            conn.execute("BEGIN IMMEDIATE")
            try:
                for version, name, checksum, sql in migrations[applied:]:
                    for statement in _sql_statements(sql):
                        conn.execute(statement)
                    conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
                                 (version, name, checksum, utc_now()))
                    conn.execute(f"PRAGMA user_version={version}")
                conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            self._validate_schema(conn)
            return {"path": str(self.path), "sqlite_version": sqlite3.sqlite_version,
                    "schema_version": migrations[-1][0], "journal_mode": actual_mode}
        finally:
            conn.close()

    @contextmanager
    def connection(self, *, write=False):
        """One snapshot per read; one short serialized transaction per write."""
        conn = self._open("rw" if write else "ro")
        try:
            self._validate_schema(conn)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if mode == "wal" and not wal_is_supported(sqlite3.sqlite_version_info):
                raise StorageError("Unsafe WAL runtime; initialize using delete before accessing data")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()

    def ensure_instrument(self, code: str, name: str | None = None) -> int:
        # Keep maintenance independent of requests/plotly.
        code = str(code).strip().lower()
        if code.startswith(("sh", "sz")):
            prefix, code = code[:2], code[2:]
        else:
            prefix = None
        if len(code) != 6 or not code.isascii() or not code.isdigit() or code[0] not in "036":
            raise ValueError("Expected a six-digit Shanghai/Shenzhen A-share code")
        exchange = "sh" if code.startswith("6") else "sz"
        if prefix and prefix != exchange:
            raise ValueError("Exchange and stock code disagree")
        with self.connection(write=True) as conn:
            conn.execute("INSERT INTO instruments(exchange, code, name, created_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(exchange, code) DO UPDATE SET name=coalesce(excluded.name, instruments.name)",
                         (exchange, code, name, utc_now()))
            return conn.execute("SELECT id FROM instruments WHERE exchange=? AND code=?",
                                (exchange, code)).fetchone()[0]

    def upsert_financial_reports(self, conn, key: SyncKey, run_id: int, rows: list[dict]) -> None:
        if not key.dataset.startswith("financial:") or key.adjustment != "raw":
            raise ValueError("Expected a financial report synchronization key")
        report_type = key.dataset.removeprefix("financial:")
        for row in rows:
            _day(row["period"])
            _day(row.get("publish_date"))
            raw_json, content_hash = _raw_payload(row["raw_json"])
            conn.execute(
                "INSERT INTO financial_reports(instrument_id,source,report_type,period,publish_date,update_time,"
                "raw_json,content_hash,obtained_at,run_id) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(instrument_id,source,report_type,period) DO UPDATE SET "
                "publish_date=excluded.publish_date,update_time=excluded.update_time,raw_json=excluded.raw_json,"
                "content_hash=excluded.content_hash,obtained_at=excluded.obtained_at,run_id=excluded.run_id",
                (key.instrument_id, key.source, report_type, row["period"], row.get("publish_date"),
                 row.get("update_time"), raw_json, content_hash, utc_now(), run_id))

    def financial_reports(self, instrument_id: int, source: str, report_type: str) -> list[dict]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM financial_reports WHERE instrument_id=? AND source=? AND report_type=? ORDER BY period",
                (instrument_id, source, report_type))]

    def upsert_dividend_events(self, conn, key: SyncKey, run_id: int, rows: list[dict]) -> None:
        if key.dataset != "dividends" or key.adjustment != "raw":
            raise ValueError("Expected a dividend synchronization key")
        for row in rows:
            for field in ("report_period", "proposal_date", "notice_date", "registration_date",
                          "ex_dividend_date"):
                _day(row.get(field))
            raw_json, content_hash = _raw_payload(row["raw_json"])
            conn.execute(
                "INSERT INTO dividend_events(instrument_id,source,event_key,source_event_id,report_period,"
                "proposal_date,notice_date,registration_date,ex_dividend_date,status,cash_per_ten,raw_json,"
                "content_hash,obtained_at,run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(instrument_id,source,event_key) DO UPDATE SET "
                "source_event_id=excluded.source_event_id,report_period=excluded.report_period,"
                "proposal_date=excluded.proposal_date,notice_date=excluded.notice_date,"
                "registration_date=excluded.registration_date,ex_dividend_date=excluded.ex_dividend_date,"
                "status=excluded.status,cash_per_ten=excluded.cash_per_ten,raw_json=excluded.raw_json,"
                "content_hash=excluded.content_hash,obtained_at=excluded.obtained_at,run_id=excluded.run_id",
                (key.instrument_id, key.source, row["event_key"], row.get("source_event_id"),
                 row.get("report_period"), row.get("proposal_date"), row.get("notice_date"),
                 row.get("registration_date"), row.get("ex_dividend_date"), row["status"],
                 row.get("cash_per_ten"), raw_json, content_hash, utc_now(), run_id))

    def dividend_events(self, instrument_id: int, source: str) -> list[dict]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM dividend_events WHERE instrument_id=? AND source=? "
                "ORDER BY coalesce(ex_dividend_date,notice_date,proposal_date),id",
                (instrument_id, source))]

    def upsert_valuation_observations(self, conn, key: SyncKey, run_id: int, rows: list[dict]) -> None:
        if not key.dataset.startswith("valuation:") or key.adjustment != "raw":
            raise ValueError("Expected a valuation synchronization key")
        metric = key.dataset.removeprefix("valuation:")
        for row in rows:
            _day(row["observed_on"])
            raw_json, content_hash = _raw_payload(row["raw_json"])
            window_value = row.get("source_windows_json", row.get("source_windows", []))
            windows = json.dumps(json.loads(window_value) if isinstance(window_value, str) else window_value,
                                 ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                "INSERT INTO valuation_observations(instrument_id,source,metric,observed_on,value,source_windows_json,"
                "sampling_version,raw_json,content_hash,obtained_at,run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(instrument_id,source,metric,observed_on) DO UPDATE SET "
                "value=excluded.value,source_windows_json=excluded.source_windows_json,"
                "sampling_version=excluded.sampling_version,raw_json=excluded.raw_json,"
                "content_hash=excluded.content_hash,obtained_at=excluded.obtained_at,run_id=excluded.run_id",
                (key.instrument_id, key.source, metric, row["observed_on"], row["value"], windows,
                 row["sampling_version"], raw_json, content_hash, utc_now(), run_id))

    def valuation_observations(self, instrument_id: int, source: str, metric: str) -> list[dict]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM valuation_observations WHERE instrument_id=? AND source=? AND metric=? ORDER BY observed_on",
                (instrument_id, source, metric))]

    def insert_industry_snapshot(self, conn, key: SyncKey, run_id: int, row: dict) -> None:
        if key.dataset != "industry" or key.adjustment != "raw":
            raise ValueError("Expected an industry synchronization key")
        raw_json, content_hash = _raw_payload(row["raw_json"])
        conn.execute(
            "INSERT INTO industry_snapshots(instrument_id,source,snapshot_at,industry_name,industry_code,"
            "classification_basis,raw_json,content_hash,run_id) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(instrument_id,source,snapshot_at) DO UPDATE SET "
            "industry_name=excluded.industry_name,industry_code=excluded.industry_code,"
            "classification_basis=excluded.classification_basis,raw_json=excluded.raw_json,"
            "content_hash=excluded.content_hash,run_id=excluded.run_id",
            (key.instrument_id, key.source, row["snapshot_at"], row["industry_name"],
             row.get("industry_code"), row["classification_basis"], raw_json, content_hash, run_id))

    def record_legacy_import(self, conn, key: SyncKey, run_id: int, path: str, content_hash: str,
                             record_count: int) -> bool:
        existing = conn.execute("SELECT content_hash FROM legacy_imports WHERE path=? AND dataset=?",
                                (str(path), key.dataset)).fetchone()
        if existing:
            if existing[0] != content_hash:
                raise StorageError("Legacy import path/dataset already records different contents")
            return False
        conn.execute("INSERT INTO legacy_imports VALUES (?,?,?,?,?,?,?)",
                     (str(path), content_hash, key.dataset, key.instrument_id, run_id, record_count, utc_now()))
        return True

    def insert_legacy_valuation_snapshot(self, conn, key: SyncKey, run_id: int, source_path: str,
                                         observed_month: str, methodology_version: str, row) -> None:
        if key.dataset != "valuation_legacy" or key.adjustment != "raw":
            raise ValueError("Expected a legacy valuation synchronization key")
        if len(observed_month) != 7:
            raise ValueError("Observed month must be YYYY-MM")
        _day(observed_month + "-01")
        raw_json, _ = _raw_payload(row)
        conn.execute(
            "INSERT INTO legacy_valuation_snapshots(instrument_id,source_path,observed_month,"
            "methodology_version,row_json,imported_at,run_id) VALUES (?,?,?,?,?,?,?)",
            (key.instrument_id, str(source_path), observed_month, methodology_version,
             raw_json, utc_now(), run_id))

    def start_sync(self, key: SyncKey, *, parser_version: str, methodology_version: str,
                   trigger_reason="refresh", requested_start=None, requested_end=None) -> int:
        _day(requested_start)
        _day(requested_end)
        if not parser_version or not methodology_version:
            raise ValueError("Parser and methodology versions are required")
        with self.connection(write=True) as conn:
            conn.execute("INSERT INTO sync_state(instrument_id, dataset, source, adjustment, data_status) "
                         "VALUES (?, ?, ?, ?, 'uninitialized') ON CONFLICT DO NOTHING", key.values())
            revision = conn.execute("SELECT revision FROM sync_state WHERE "
                                    "instrument_id=? AND dataset=? AND source=? AND adjustment=?", key.values()).fetchone()[0]
            return conn.execute(
                "INSERT INTO sync_runs(instrument_id, dataset, source, adjustment, parser_version, "
                "methodology_version, trigger_reason, requested_start, requested_end, started_at, status, base_revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)",
                (*key.values(), parser_version, methodology_version, trigger_reason,
                 requested_start, requested_end, utc_now(), revision)).lastrowid

    def fail_sync(self, run_id: int, error: str, *, superseded=False):
        with self.connection(write=True) as conn:
            changed = conn.execute("UPDATE sync_runs SET status=?, finished_at=?, error=? "
                                   "WHERE id=? AND status='running'",
                                   ("superseded" if superseded else "failed", utc_now(), str(error)[:2000], run_id)).rowcount
            if changed != 1:
                raise StorageError("Synchronization run is missing or already finished")

    def complete_sync(self, run_id: int, result: SyncResult, write_records: Callable[[sqlite3.Connection], None]):
        """Atomically commit caller's validated facts, successful run and state.

        write_records must only issue local SQL on this connection. Do not perform
        source requests here. Domain validation/UPSERT policies belong to P2/P3.
        """
        try:
            result.validate()
            with self.connection(write=True) as conn:
                run = conn.execute("SELECT * FROM sync_runs WHERE id=? AND status='running'", (run_id,)).fetchone()
                if run is None:
                    raise StorageError("Synchronization run is missing or already finished")
                key = tuple(run[f] for f in ("instrument_id", "dataset", "source", "adjustment"))
                state = conn.execute("SELECT * FROM sync_state WHERE "
                                     "instrument_id=? AND dataset=? AND source=? AND adjustment=?", key).fetchone()
                if state["revision"] != run["base_revision"]:
                    raise StaleSyncError("A newer synchronization has already committed")
                if result.no_data and state["data_status"] == "data":
                    raise StorageError("No-data response cannot replace previously known data")
                if state["data_status"] == "data" and (
                        result.coverage_start > state["coverage_start"]
                        or result.coverage_end < state["coverage_end"]
                        or result.data_watermark < state["data_watermark"]):
                    raise StorageError("Successful coverage/watermark cannot move backward")
                write_records(conn)
                now = utc_now()
                conn.execute("UPDATE sync_runs SET status=?, finished_at=?, record_count=? WHERE id=?",
                             ("no_data" if result.no_data else "success", now, result.record_count, run_id))
                conn.execute(
                    "UPDATE sync_state SET revision=revision+1, data_status=?, coverage_start=?, coverage_end=?, "
                    "data_watermark=?, checked_at=?, succeeded_at=?, next_full_audit_at=?, "
                    "last_success_run_id=?, active_price_version_id=? WHERE "
                    "instrument_id=? AND dataset=? AND source=? AND adjustment=?",
                    ("no_data" if result.no_data else "data", result.coverage_start, result.coverage_end,
                     result.data_watermark, result.checked_at or now, now, result.next_full_audit_at, run_id,
                     result.active_price_version_id if result.active_price_version_id is not None
                     else state["active_price_version_id"], *key))
        except Exception as exc:
            # The facts/state transaction has rolled back before recording failure.
            with self.connection() as conn:
                running = conn.execute("SELECT status FROM sync_runs WHERE id=?", (run_id,)).fetchone()
            if running is not None and running["status"] == "running":
                self.fail_sync(run_id, str(exc), superseded=isinstance(exc, StaleSyncError))
            raise

    def recover_interrupted_runs(self) -> int:
        """Only on startup under the exclusive instance lock, before any workers."""
        with self.connection(write=True) as conn:
            recovered = conn.execute("UPDATE sync_runs SET status='failed', finished_at=?, "
                                     "error='Application exited before synchronization completed' "
                                     "WHERE status='running'", (utc_now(),)).rowcount
            conn.execute("DELETE FROM adjusted_price_versions WHERE status='candidate' AND run_id IN "
                         "(SELECT id FROM sync_runs WHERE status IN ('failed','superseded'))")
            return recovered

    def check(self, *, current=True):
        conn = self._open("ro")
        try:
            conn.execute("BEGIN")
            schema_count = self._validate_schema(conn, current=current)
            integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != ["ok"] or foreign_keys:
                raise StorageError(f"Database integrity failed: {integrity}; foreign keys: {len(foreign_keys)}")
            invalid_runs = conn.execute(
                "SELECT count(*) FROM sync_state s LEFT JOIN sync_runs r ON r.id=s.last_success_run_id "
                "WHERE (s.data_status<>'uninitialized' AND s.last_success_run_id IS NULL) OR "
                "(s.data_status='uninitialized' AND s.last_success_run_id IS NOT NULL) OR "
                "r.status NOT IN ('success','no_data') OR "
                "(s.data_status='data' AND r.status<>'success') OR "
                "(s.data_status='no_data' AND r.status<>'no_data')").fetchone()[0]
            invalid_versions = conn.execute(
                "SELECT count(*) FROM adjusted_price_versions v WHERE v.status='complete' AND "
                "(v.row_count <> (SELECT count(*) FROM adjusted_daily_prices p WHERE p.version_id=v.id) "
                "OR v.coverage_start <> (SELECT min(trade_date) FROM adjusted_daily_prices p WHERE p.version_id=v.id) "
                "OR v.coverage_end <> (SELECT max(trade_date) FROM adjusted_daily_prices p WHERE p.version_id=v.id))").fetchone()[0]
            invalid_active = conn.execute(
                "SELECT count(*) FROM sync_state s JOIN adjusted_price_versions v ON v.id=s.active_price_version_id "
                "JOIN sync_runs r ON r.id=v.run_id "
                "WHERE v.status<>'complete' OR s.coverage_start IS NULL OR s.coverage_end IS NULL "
                "OR v.coverage_start<>s.coverage_start OR v.coverage_end<>s.coverage_end "
                "OR r.status<>'success'").fetchone()[0]
            invalid_provenance = 0
            for table, dataset in (("financing_daily", "financing"),
                                   ("shareholder_observations", "shareholders"),
                                   ("raw_daily_prices", "prices_raw"),
                                   ("adjusted_price_versions", "prices_adjusted")):
                invalid_provenance += conn.execute(
                    f"SELECT count(*) FROM {table} f JOIN sync_runs r ON r.id=f.run_id "
                    "WHERE f.instrument_id<>r.instrument_id OR f.source<>r.source OR r.dataset<>? "
                    + ("OR r.status<>'success' OR r.adjustment<>'raw'" if table != "adjusted_price_versions"
                       else "OR r.adjustment<>f.adjustment"),
                    (dataset,)).fetchone()[0]
            if schema_count >= 3:
                p4_rules = (
                    ("financial_reports", "r.dataset<>'financial:'||f.report_type OR r.source<>f.source"),
                    ("dividend_events", "r.dataset<>'dividends' OR r.source<>f.source"),
                    ("valuation_observations", "r.dataset<>'valuation:'||f.metric OR r.source<>f.source"),
                    ("industry_snapshots", "r.dataset<>'industry' OR r.source<>f.source"),
                    ("legacy_valuation_snapshots", "r.dataset<>'valuation_legacy'"),
                    ("legacy_imports", "r.dataset<>f.dataset"),
                )
                for table, rule in p4_rules:
                    invalid_provenance += conn.execute(
                        f"SELECT count(*) FROM {table} f JOIN sync_runs r ON r.id=f.run_id "
                        f"WHERE f.instrument_id<>r.instrument_id OR {rule} OR "
                        + ("r.status NOT IN ('success','no_data')" if table == "legacy_imports"
                           else "r.status<>'success'")
                    ).fetchone()[0]
                invalid_provenance += conn.execute(
                    "SELECT count(*) FROM report_overrides f LEFT JOIN sync_runs r ON r.id=f.run_id "
                    "WHERE (f.origin='manual' AND f.run_id IS NOT NULL) "
                    "OR (f.origin='legacy' AND (r.id IS NULL OR r.instrument_id<>f.instrument_id "
                    "OR r.dataset<>'report_overrides' OR r.status<>'success'))"
                ).fetchone()[0]
            if invalid_runs or invalid_versions or invalid_active or invalid_provenance:
                raise StorageError("Successful synchronization/price version relationships are inconsistent")
            result = {"path": str(self.path), "sqlite_version": sqlite3.sqlite_version,
                    "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
                    "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                    "integrity": "ok", "instruments": conn.execute("SELECT count(*) FROM instruments").fetchone()[0]}
            if schema_count >= 3:
                result["p4_facts"] = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                                      for table in ("financial_reports", "dividend_events", "valuation_observations",
                                                    "industry_snapshots", "report_overrides", "legacy_valuation_snapshots")}
            return result
        finally:
            conn.close()

    def backup(self, destination: Path | str) -> Path:
        """Online Backup API, validate a self-contained DELETE-mode copy before publish."""
        destination = Path(destination).resolve()
        if destination == self.path or destination.exists():
            raise StorageError("Backup destination must be a new file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".sqlite-backup-", suffix=".tmp", dir=destination.parent)
        os.close(fd)
        temporary = Path(temp_name)
        source = target = None
        try:
            source = self._open("ro")
            self._validate_schema(source, current=False)
            target = sqlite3.connect(temporary)
            source.backup(target, pages=256, sleep=0.05)
            target.execute("PRAGMA journal_mode=DELETE")
            target.close()
            target = None
            Database(temporary, journal_mode="delete").check(current=False)
            # Avoid overwriting a backup created concurrently by another process.
            os.link(temporary, destination)
            return destination
        finally:
            if target is not None:
                target.close()
            if source is not None:
                source.close()
            temporary.unlink(missing_ok=True)

    def daily_backup(self, directory: Path | str | None = None, *, keep=7, day: date | None = None, replace=False) -> Path:
        if type(keep) is not int or keep < 1:
            raise ValueError("keep must be positive")
        day = day or datetime.now(timezone.utc).date()
        directory = Path(directory).resolve() if directory else self.path.parent / "backups"
        path = directory / f"{self.path.stem}-daily-{day.isoformat()}.sqlite3"
        if replace and path.exists():
            candidate = directory / f".daily-candidate-{uuid4().hex}.sqlite3"
            self.backup(candidate)
            candidate.replace(path)
        elif not path.exists():
            self.backup(path)
        else:
            Database(path, journal_mode="delete").check(current=False)
        # Only the exact daily names owned by this database; pre-migration files stay.
        owned = []
        for candidate in directory.glob(f"{self.path.stem}-daily-*.sqlite3"):
            token = candidate.name[len(self.path.stem) + len("-daily-"):-len(".sqlite3")]
            try:
                _day(token)
            except ValueError:
                continue
            owned.append(candidate)
        for candidate in sorted(owned, reverse=True)[keep:]:
            candidate.unlink()
        return path


@contextmanager
def instance_lock(database_path: Path | str):
    """OS lock is released on crash; the persistent lock file is not a stale lock."""
    path = Path(str(Path(database_path).resolve()) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    locked = False
    try:
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise StorageError("Another application/maintenance instance is using this database") from exc
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def restore_backup(source: Path | str, destination: Path | str) -> Path:
    """Restore to a new file only; never overwrite a running or valuable database."""
    destination = Path(destination).resolve()
    with instance_lock(destination):
        if destination.exists() or any(Path(str(destination) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise StorageError("Restore destination or its sidecars already exist; restore to a new path")
        return Database(source, journal_mode="delete").backup(destination)


def main():
    parser = argparse.ArgumentParser(description="SQLite initialization, verification and consistent backup")
    parser.add_argument("command", choices=("init", "check", "backup", "restore"))
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--journal-mode", choices=("auto", "delete", "wal"), default="auto")
    parser.add_argument("--output", type=Path, help="New file for backup/restore")
    args = parser.parse_args()
    db = Database(args.database, journal_mode=args.journal_mode)
    if args.command == "init":
        with instance_lock(db.path):
            result = db.initialize()
    elif args.command == "check":
        result = db.check()
    else:
        if args.output is None:
            parser.error("backup/restore require --output (a new file)")
        result = {"path": str(db.backup(args.output) if args.command == "backup"
                              else restore_backup(db.path, args.output))}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
