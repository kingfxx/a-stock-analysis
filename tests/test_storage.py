import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest

from quarterly_dashboard import storage
from quarterly_dashboard.storage import (Database, StorageError, StaleSyncError, SyncKey,
                                         SyncResult, instance_lock, restore_backup, utc_now)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "analysis.sqlite3")
    with instance_lock(database.path):
        database.initialize()
    return database


def sync(db, code="000001", dataset="financing", source="eastmoney", adjustment="raw"):
    instrument = db.ensure_instrument(code)
    key = SyncKey(instrument, dataset, source, adjustment)
    run = db.start_sync(key, parser_version="parser-v1", methodology_version="method-v1")
    return key, run


def write_financing(conn, key, run, day="2026-09-29", balance=0):
    conn.execute("INSERT INTO financing_daily(instrument_id,source,trade_date,margin_balance,net_buy,"
                 "raw_json,content_hash,obtained_at,run_id) VALUES (?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(instrument_id,source,trade_date) DO UPDATE SET "
                 "margin_balance=excluded.margin_balance,run_id=excluded.run_id",
                 (key.instrument_id, key.source, day, balance, -30, '{"RQMCL":67900}',
                  "sample-hash", utc_now(), run))


def commit_financing(db, key, run, day="2026-09-29", balance=0):
    db.complete_sync(run, SyncResult(1, day, day, day),
                     lambda conn: write_financing(conn, key, run, day, balance))


def get_state(db, key):
    with db.connection() as conn:
        return dict(conn.execute("SELECT * FROM sync_state WHERE instrument_id=? AND dataset=? "
                                 "AND source=? AND adjustment=?", key.values()).fetchone())


@pytest.mark.parametrize("version,safe", [
    ((3, 35, 5), False), ((3, 44, 5), False), ((3, 44, 6), True), ((3, 44, 7), True),
    ((3, 45, 0), False), ((3, 50, 6), False), ((3, 50, 7), True),
    ((3, 51, 0), False), ((3, 51, 2), False), ((3, 51, 3), True), ((3, 52, 0), True),
])
def test_journal_selection_uses_actual_fixed_sqlite_versions(version, safe):
    assert storage.choose_journal_mode("auto", version) == ("wal" if safe else "delete")
    assert storage.choose_journal_mode("delete", version) == "delete"
    if safe:
        assert storage.choose_journal_mode("wal", version) == "wal"
    else:
        with pytest.raises(StorageError, match="WAL-reset"):
            storage.choose_journal_mode("wal", version)


def test_schema_is_idempotent_preserves_text_codes_and_closes_connections(db):
    first = db.ensure_instrument("000001", "平安银行")
    with instance_lock(db.path):
        db.initialize()
    assert db.ensure_instrument("sz000001") == first
    with db.connection() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("SELECT code, name FROM instruments").fetchone()[:] == ("000001", "平安银行")
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM instruments")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    assert db.check()["integrity"] == "ok"
    with pytest.raises(ValueError):
        db.ensure_instrument("sh000001")


def test_unrelated_future_or_modified_migration_database_is_rejected(tmp_path, db):
    unrelated = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(unrelated) as conn:
        conn.execute("CREATE TABLE valuable_data(value TEXT)")
    with pytest.raises(StorageError, match="Not an"):
        Database(unrelated).initialize()
    with db.connection(write=True) as conn:
        conn.execute("PRAGMA user_version=99")
    with pytest.raises(StorageError):
        db.initialize()
    # Restore header solely to exercise checksum rejection, not production repair.
    with sqlite3.connect(db.path) as conn:
        conn.execute("PRAGMA user_version=1")
        conn.execute("UPDATE schema_migrations SET checksum='changed'")
    with pytest.raises(StorageError, match="changed"):
        db.initialize()


def test_migration_checksums_survive_git_lf_crlf_checkout(db, tmp_path, monkeypatch):
    sql = (Path(storage.__file__).parent / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
    directory = tmp_path / "alternate-checkout" / "migrations"
    directory.mkdir(parents=True)
    (directory / "001_initial.sql").write_bytes(sql.replace("\n", "\r\n").encode("utf-8"))
    monkeypatch.setattr(storage, "__file__", str(directory.parent / "storage.py"))
    with instance_lock(db.path):
        db.initialize()
    assert db.check()["schema_version"] == 1


def test_failed_schema_upgrade_rolls_back_ddl_and_keeps_pre_migration_backup(db, monkeypatch):
    original = list(storage._migration_files())
    original.append((2, "002_fault.sql", "test-checksum",
                     "CREATE TABLE should_rollback(value TEXT);\nINSERT INTO nonexistent VALUES (1);\n"))
    monkeypatch.setattr(storage, "_migration_files", lambda: iter(original))
    with pytest.raises(sqlite3.OperationalError):
        with instance_lock(db.path):
            db.initialize()
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
    backups = list((db.path.parent / "backups").glob("pre-migration-*.sqlite3"))
    assert len(backups) == 1
    assert Database(backups[0]).check(current=False)["integrity"] == "ok"


@pytest.mark.parametrize("interruption", ["exception", "interrupt"])
def test_write_transaction_rolls_back_on_exception_or_interrupt(db, interruption):
    error = RuntimeError if interruption == "exception" else KeyboardInterrupt
    with pytest.raises(error):
        with db.connection(write=True) as conn:
            conn.execute("INSERT INTO instruments(exchange,code,created_at) VALUES ('sz','000001',?)", (utc_now(),))
            raise error("interrupted")
    assert db.check()["instruments"] == 0


def test_successful_facts_and_watermark_commit_together_with_source_fields(db):
    key, run = sync(db)
    commit_financing(db, key, run)
    state = get_state(db, key)
    assert state["data_watermark"] == "2026-09-29"
    assert state["last_success_run_id"] == run
    assert state["revision"] == 1
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM financing_daily").fetchone()
        assert row["margin_balance"] == 0
        assert row["net_buy"] == -30
        assert json.loads(row["raw_json"])["RQMCL"] == 67900
        assert conn.execute("SELECT status FROM sync_runs").fetchone()[0] == "success"


def test_failed_batch_preserves_facts_watermark_and_records_failure(db):
    key, run = sync(db)
    commit_financing(db, key, run, balance=100)
    original = get_state(db, key)
    _, next_run = sync(db)
    def fail_midway(conn):
        write_financing(conn, key, next_run, balance=999)
        write_financing(conn, key, next_run, "2026-09-30", 999)
        raise ValueError("bad candidate")
    with pytest.raises(ValueError, match="bad candidate"):
        db.complete_sync(next_run, SyncResult(2, "2026-09-29", "2026-09-30", "2026-09-30"), fail_midway)
    assert get_state(db, key) == original
    with db.connection() as conn:
        assert conn.execute("SELECT margin_balance FROM financing_daily").fetchall()[0][0] == 100
        assert conn.execute("SELECT count(*) FROM financing_daily").fetchone()[0] == 1
        assert tuple(conn.execute("SELECT status,error FROM sync_runs WHERE id=?", (next_run,)).fetchone()) == (
            "failed", "bad candidate")


def test_foreign_key_failure_rolls_back_whole_sync(db):
    key, run = sync(db)
    def invalid(conn):
        write_financing(conn, key, run)
        conn.execute("INSERT INTO raw_daily_prices(instrument_id,source,trade_date,close,raw_json,"
                     "content_hash,obtained_at,run_id) VALUES (999,'qq','2026-09-29',1,'{}','hash',?,?)", (utc_now(), run))
    with pytest.raises(sqlite3.IntegrityError):
        db.complete_sync(run, SyncResult(1, "2026-09-29", "2026-09-29", "2026-09-29"), invalid)
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM financing_daily").fetchone()[0] == 0
    assert get_state(db, key)["data_watermark"] is None


def test_stale_run_cannot_overwrite_newer_success(db):
    key, first = sync(db)
    _, later = sync(db)
    commit_financing(db, key, later, balance=200)
    with pytest.raises(StaleSyncError):
        commit_financing(db, key, first, balance=100)
    assert get_state(db, key)["last_success_run_id"] == later
    with db.connection() as conn:
        assert conn.execute("SELECT margin_balance FROM financing_daily").fetchone()[0] == 200
        assert conn.execute("SELECT status FROM sync_runs WHERE id=?", (first,)).fetchone()[0] == "superseded"


def test_no_data_is_explicit_and_cannot_replace_known_facts(db):
    key, run = sync(db)
    db.complete_sync(run, SyncResult(0, no_data=True), lambda conn: None)
    assert get_state(db, key)["data_status"] == "no_data"
    _, next_run = sync(db)
    commit_financing(db, key, next_run)
    original = get_state(db, key)
    _, empty = sync(db)
    with pytest.raises(StorageError, match="No-data"):
        db.complete_sync(empty, SyncResult(0, no_data=True), lambda conn: None)
    assert get_state(db, key) == original


def test_success_cannot_shrink_history_or_use_non_utc_audit_time(db):
    key, run = sync(db)
    commit_financing(db, key, run)
    original = get_state(db, key)
    _, old = sync(db)
    with pytest.raises(StorageError, match="backward"):
        db.complete_sync(old, SyncResult(1, "2026-09-28", "2026-09-28", "2026-09-28"), lambda conn: None)
    assert get_state(db, key) == original
    with pytest.raises(ValueError, match="watermark"):
        SyncResult(0).validate()
    with pytest.raises(ValueError, match="UTC"):
        SyncResult(1, "2026-09-29", "2026-09-29", "2026-09-29",
                   next_full_audit_at="2026-10-30T00:00:00+08:00").validate()
    SyncResult(1, "2026-09-29", "2026-09-29", "2026-09-29",
               next_full_audit_at="2026-10-30T00:00:00Z").validate()


def test_crash_recovery_fails_running_runs_only_without_advancing_watermark(db):
    key, first = sync(db)
    commit_financing(db, key, first)
    original = get_state(db, key)
    _, interrupted = sync(db)
    with instance_lock(db.path):
        assert db.recover_interrupted_runs() == 1
        assert db.recover_interrupted_runs() == 0
    assert get_state(db, key) == original
    with db.connection() as conn:
        assert conn.execute("SELECT status FROM sync_runs WHERE id=?", (interrupted,)).fetchone()[0] == "failed"


def test_parallel_thread_connections_serialize_atomic_commits(db):
    def update(code):
        key, run = sync(db, code)
        commit_financing(db, key, run)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(update, ["000001", "600887", "601919", "300750"]))
    assert db.check()["instruments"] == 4
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM financing_daily").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM sync_state WHERE revision=1 AND data_watermark='2026-09-29'").fetchone()[0] == 4


def price_candidate(conn, key, run, rows):
    version = conn.execute("INSERT INTO adjusted_price_versions(instrument_id,source,adjustment,source_basis,status,"
                           "coverage_start,coverage_end,row_count,created_at,run_id) "
                           "VALUES (?,?,'qfq','qq-qfq','candidate',?,?,?,?,?)",
                           (key.instrument_id, key.source, rows[0][0], rows[-1][0], len(rows), utc_now(), run)).lastrowid
    conn.executemany("INSERT INTO adjusted_daily_prices VALUES (?,?,?,?)",
                     [(version, day, close, "{}") for day, close in rows])
    return version


def test_price_version_validates_coverage_and_is_immutable_after_publication(db):
    key, run = sync(db, dataset="prices_adjusted", source="qq", adjustment="qfq")
    with db.connection(write=True) as conn:
        version = price_candidate(conn, key, run, [("2026-09-28", -1.5), ("2026-09-29", 0)])
        conn.execute("UPDATE adjusted_price_versions SET status='complete',validated_at=? WHERE id=?", (utc_now(), version))
    db.complete_sync(run, SyncResult(2, "2026-09-28", "2026-09-29", "2026-09-29", active_price_version_id=version), lambda conn: None)
    assert db.check()["integrity"] == "ok"
    for sql in ("UPDATE adjusted_daily_prices SET close=123 WHERE version_id=?",
                "DELETE FROM adjusted_daily_prices WHERE version_id=?",
                "UPDATE adjusted_price_versions SET row_count=999 WHERE id=?"):
        with pytest.raises(sqlite3.IntegrityError):
            with db.connection(write=True) as conn:
                conn.execute(sql, (version,))


def test_incomplete_or_wrong_stock_price_version_cannot_be_activated(db):
    key, run = sync(db, dataset="prices_adjusted", source="qq", adjustment="qfq")
    with db.connection(write=True) as conn:
        version = price_candidate(conn, key, run, [("2026-09-29", 1)])
    with pytest.raises(sqlite3.IntegrityError, match="complete"):
        db.complete_sync(run, SyncResult(1, "2026-09-29", "2026-09-29", "2026-09-29", active_price_version_id=version), lambda conn: None)
    with db.connection(write=True) as conn:
        conn.execute("UPDATE adjusted_price_versions SET row_count=2 WHERE id=?", (version,))
        with pytest.raises(sqlite3.IntegrityError, match="coverage"):
            conn.execute("UPDATE adjusted_price_versions SET status='complete',validated_at=? WHERE id=?", (utc_now(), version))
    assert get_state(db, key)["active_price_version_id"] is None


def test_completed_price_pointer_requires_matching_stock_and_coverage(db):
    key, run = sync(db, dataset="prices_adjusted", source="qq", adjustment="qfq")
    with db.connection(write=True) as conn:
        version = price_candidate(conn, key, run, [("2026-09-29", 1)])
        conn.execute("UPDATE adjusted_price_versions SET status='complete',validated_at=? WHERE id=?", (utc_now(), version))
    other, other_run = sync(db, "600887", dataset="prices_adjusted", source="qq", adjustment="qfq")
    with pytest.raises(sqlite3.IntegrityError):
        db.complete_sync(other_run, SyncResult(1, "2026-09-29", "2026-09-29", "2026-09-29", active_price_version_id=version), lambda conn: None)
    with pytest.raises(sqlite3.IntegrityError):
        db.complete_sync(run, SyncResult(1, "2026-09-28", "2026-09-29", "2026-09-29", active_price_version_id=version), lambda conn: None)
    assert get_state(db, key)["active_price_version_id"] is None
    assert get_state(db, other)["active_price_version_id"] is None


def test_facts_cannot_reference_another_stock_or_source_run(db):
    key, run = sync(db)
    other, _ = sync(db, "600887")
    with pytest.raises(sqlite3.IntegrityError, match="provenance"):
        db.complete_sync(run, SyncResult(1, "2026-09-29", "2026-09-29", "2026-09-29"),
                         lambda conn: write_financing(conn, other, run))
    assert get_state(db, key)["revision"] == 0


def test_old_unreferenced_price_version_can_be_removed_as_a_whole(db):
    key, run = sync(db, dataset="prices_adjusted", source="qq", adjustment="qfq")
    with db.connection(write=True) as conn:
        version = price_candidate(conn, key, run, [("2026-09-29", 0)])
        conn.execute("UPDATE adjusted_price_versions SET status='complete',validated_at=? WHERE id=?", (utc_now(), version))
        conn.execute("DELETE FROM adjusted_price_versions WHERE id=?", (version,))
        assert conn.execute("SELECT count(*) FROM adjusted_daily_prices").fetchone()[0] == 0


def test_backup_restore_preserves_rows_and_state_and_refuses_overwrite(db, tmp_path):
    key, run = sync(db)
    commit_financing(db, key, run, balance=123)
    original = get_state(db, key)
    path = db.backup(tmp_path / "backup.sqlite3")
    _, newer = sync(db)
    commit_financing(db, key, newer, balance=456)
    restored = Database(restore_backup(path, tmp_path / "restored.sqlite3"))
    assert restored.check()["integrity"] == "ok"
    assert get_state(restored, key) == original
    with restored.connection() as conn:
        assert conn.execute("SELECT margin_balance FROM financing_daily").fetchone()[0] == 123
    with pytest.raises(StorageError, match="new file"):
        db.backup(path)
    with pytest.raises(StorageError, match="already exist"):
        restore_backup(path, db.path)


def test_backup_rejects_inconsistent_business_state_and_does_not_publish(db, tmp_path):
    key, run = sync(db)
    # Simulate a buggy consumer bypassing the transaction API.
    with db.connection(write=True) as conn:
        conn.execute("UPDATE sync_state SET data_status='data',last_success_run_id=?", (run,))
    with pytest.raises(StorageError, match="relationships"):
        db.backup(tmp_path / "invalid.sqlite3")
    assert not (tmp_path / "invalid.sqlite3").exists()
    assert not list(tmp_path.glob(".sqlite-backup-*"))


def test_online_backup_during_updates_keeps_facts_and_state_in_one_snapshot(db, tmp_path):
    key, run = sync(db)
    def commit_pair(run, balance):
        def write(conn):
            write_financing(conn, key, run, "2026-09-28", balance)
            write_financing(conn, key, run, "2026-09-29", balance)
        db.complete_sync(run, SyncResult(2, "2026-09-28", "2026-09-29", "2026-09-29"), write)
    commit_pair(run, 1)
    def update():
        for balance in range(2, 12):
            _, next_run = sync(db)
            commit_pair(next_run, balance)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(update)
        for index in range(3):
            copy = Database(db.backup(tmp_path / f"live-{index}.sqlite3"))
            assert copy.check()["integrity"] == "ok"
            with copy.connection() as conn:
                balances = [r[0] for r in conn.execute("SELECT margin_balance FROM financing_daily")]
                revision = conn.execute("SELECT revision FROM sync_state").fetchone()[0]
                assert balances == [revision, revision]
        task.result(timeout=5)


def test_daily_backup_retention_only_removes_owned_daily_files(db, tmp_path):
    directory = tmp_path / "backups"
    first = db.daily_backup(directory, day=date(2026, 9, 1), keep=2)
    original = first.read_bytes()
    assert db.daily_backup(directory, day=date(2026, 9, 1), keep=2).read_bytes() == original
    special = directory / "pre-migration-keep.sqlite3"
    special.write_bytes(original)
    other = directory / "analysis-daily-unrelated.sqlite3"
    other.write_bytes(original)
    for offset in range(1, 4):
        db.daily_backup(directory, day=date(2026, 9, 1) + timedelta(days=offset), keep=2)
    assert not first.exists()
    assert special.exists() and other.exists()
    assert sorted(p.name for p in directory.glob("analysis-daily-2026*.sqlite3")) == [
        "analysis-daily-2026-09-03.sqlite3", "analysis-daily-2026-09-04.sqlite3"]


def test_instance_lock_prevents_other_process_and_releases_after_exception(db):
    command = [sys.executable, "-c",
               "from quarterly_dashboard.storage import instance_lock; import sys; "
               "exec('with instance_lock(sys.argv[1]):\\n print(\"acquired\")')", str(db.path)]
    with pytest.raises(RuntimeError):
        with instance_lock(db.path):
            result = subprocess.run(command, capture_output=True, text=True)
            assert result.returncode != 0
            assert "Another application" in result.stderr
            raise RuntimeError("exit")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "acquired" in result.stdout


def test_maintenance_cli_requires_no_runtime_dependencies(db, tmp_path):
    result = subprocess.run([sys.executable, "-m", "quarterly_dashboard.storage", "check",
                             "--database", str(db.path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["integrity"] == "ok"
    result = subprocess.run([sys.executable, "-m", "quarterly_dashboard.storage", "backup",
                             "--database", str(db.path), "--output", str(tmp_path / "cli.sqlite3")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
