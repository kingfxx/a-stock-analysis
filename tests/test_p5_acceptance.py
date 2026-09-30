"""P5 storage acceptance checks against a disposable database.

These run with the host's selected journal mode. A fixed-SQLite WAL run must be
performed separately before claiming WAL acceptance.
"""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from quarterly_dashboard.storage import (
    Database, SyncKey, SyncResult, instance_lock, restore_backup, utc_now,
)


DAY = "2026-09-29"


def _seed(db):
    instrument = db.ensure_instrument("000001")
    key = SyncKey(instrument, "financing", "eastmoney", "raw")
    run = db.start_sync(key, parser_version="p5", methodology_version="p5")
    _commit(db, key, run, 1)
    return key


def _commit(db, key, run, balance, entered=None):
    def write(conn):
        conn.execute(
            "INSERT INTO financing_daily(instrument_id,source,trade_date,margin_balance,"
            "raw_json,content_hash,obtained_at,run_id) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(instrument_id,source,trade_date) DO UPDATE SET "
            "margin_balance=excluded.margin_balance,raw_json=excluded.raw_json,"
            "content_hash=excluded.content_hash,obtained_at=excluded.obtained_at,"
            "run_id=excluded.run_id",
            (key.instrument_id, key.source, DAY, balance, "{}", str(balance), utc_now(), run),
        )
        if entered:
            entered.set()

    db.complete_sync(run, SyncResult(1, DAY, DAY, DAY), write)


def _snapshot(db):
    with db.connection() as conn:
        return tuple(conn.execute(
            "SELECT f.margin_balance,s.revision,s.last_success_run_id "
            "FROM financing_daily f JOIN sync_state s ON "
            "s.instrument_id=f.instrument_id AND s.source=f.source "
            "WHERE f.trade_date=? AND s.dataset='financing' AND s.adjustment='raw'",
            (DAY,),
        ).fetchone())


def test_process_crash_rolls_back_uncommitted_fact_and_restores_last_success(tmp_path):
    db = Database(tmp_path / "analysis.sqlite3")
    with instance_lock(db.path):
        db.initialize()
    key = _seed(db)
    before = _snapshot(db)
    interrupted = db.start_sync(key, parser_version="p5", methodology_version="p5")

    # Exit the child without a SQLite commit or Python context-manager cleanup.
    child = (
        "import os,sqlite3,sys; "
        "c=sqlite3.connect(sys.argv[1],isolation_level=None); "
        "c.execute('BEGIN IMMEDIATE'); "
        "c.execute('UPDATE financing_daily SET margin_balance=999'); "
        "os._exit(17)"
    )
    result = subprocess.run([sys.executable, "-c", child, str(db.path)], capture_output=True)
    assert result.returncode == 17, result.stderr
    assert _snapshot(db) == before

    with instance_lock(db.path):
        assert db.recover_interrupted_runs() == 1
    with db.connection() as conn:
        assert conn.execute("SELECT status FROM sync_runs WHERE id=?", (interrupted,)).fetchone()[0] == "failed"

    backup = db.backup(tmp_path / "backup.sqlite3")
    restored = Database(restore_backup(backup, tmp_path / "restored.sqlite3"))
    assert restored.check()["integrity"] == "ok"
    assert _snapshot(restored) == before


def test_reader_snapshot_and_online_backup_keep_fact_and_watermark_together(tmp_path):
    db = Database(tmp_path / "analysis.sqlite3")
    with instance_lock(db.path):
        db.initialize()
    key = _seed(db)
    before = _snapshot(db)
    entered = Event()
    run = db.start_sync(key, parser_version="p5", methodology_version="p5")

    # Keep a read transaction open while a second thread prepares a new fact.
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.connection() as reader:
            old_balance = reader.execute("SELECT margin_balance FROM financing_daily").fetchone()[0]
            task = pool.submit(_commit, db, key, run, 2, entered)
            assert entered.wait(3)
            old_revision = reader.execute("SELECT revision FROM sync_state WHERE "
                                          "instrument_id=? AND dataset='financing' AND "
                                          "source='eastmoney' AND adjustment='raw'",
                                          (key.instrument_id,)).fetchone()[0]
            assert (old_balance, old_revision) == before[:2]
            # The reader still sees the old revision until its snapshot ends.
        task.result(timeout=5)
    after = _snapshot(db)
    assert after[0] == 2 and after[1] == before[1] + 1 and after[2] == run
    backup = db.backup(tmp_path / "live.sqlite3")
    restored = Database(restore_backup(backup, tmp_path / "restored.sqlite3"))
    assert _snapshot(restored) == after
    assert restored.check()["integrity"] == "ok"
