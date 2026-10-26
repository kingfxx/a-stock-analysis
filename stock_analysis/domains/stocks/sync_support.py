"""Shared stock-data synchronization policy, locks and state helpers."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from threading import Lock
from stock_analysis.domains.stocks.source_parsers import normalize_code
from stock_analysis.infrastructure.storage import utc_now

_LOCKS, _LOCKS_GUARD = {}, Lock()
AUDIT_DAYS = {"financial": 180, "business_segments": 180, "valuation": 90, "prices_adjusted": 30}

def data_lock(db, dataset, code):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault((str(db.path), dataset, code), Lock())


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def instrument_id(db, code):
    with db.connection() as conn:
        row = conn.execute("SELECT id FROM instruments WHERE code=?", (normalize_code(code),)).fetchone()
        return row[0] if row else None


def sync_state(db, key):
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM sync_state WHERE instrument_id=? AND dataset=? AND source=? AND adjustment=?",
                           key.values()).fetchone()
        return dict(row) if row else {}


def recently_checked(state, seconds=600):
    checked = state.get("checked_at")
    return bool(checked and (datetime.now(timezone.utc) - datetime.fromisoformat(checked)).total_seconds() < seconds)


def recent_sync_failure(db, key, seconds=600, *, now=None):
    """Return a still-current failure inside the automatic retry quiet period."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = (current - timedelta(seconds=seconds)).isoformat()
    with db.connection() as conn:
        row = conn.execute("SELECT id,error,finished_at,status FROM sync_runs WHERE instrument_id=? AND dataset=? "
                           "AND source=? AND adjustment=? AND status IN ('failed','success','no_data','superseded') "
                           "ORDER BY finished_at DESC,id DESC LIMIT 1", key.values()).fetchone()
    return dict(row) if row and row['status'] == 'failed' and row['finished_at'] >= cutoff else None


def checked_today(state):
    """Daily source checks follow the Shanghai calendar, not a rolling TTL."""
    checked = state.get("checked_at")
    if not checked:
        return False
    local = timezone(timedelta(hours=8))
    stamp = datetime.fromisoformat(checked.replace("Z", "+00:00"))
    return stamp.astimezone(local).date() == datetime.now(local).date()


def audit_due(state, dataset="prices_adjusted"):
    if dataset.split(":", 1)[0] not in AUDIT_DAYS:
        return False
    return not state.get("next_full_audit_at") or state["next_full_audit_at"] <= utc_now()


def next_audit(dataset="prices_adjusted"):
    days = AUDIT_DAYS.get(dataset.split(":", 1)[0])
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days else None


def mark_failed(db, run, exc, *, collection_batch_id=None):
    with db.connection() as conn:
        row = conn.execute("SELECT status FROM sync_runs WHERE id=?", (run,)).fetchone()
    if row and row[0] == "running":
        db.fail_sync(run, exc, collection_batch_id=collection_batch_id)


def backup_before_update(db):
    """Keep the first daily snapshot; serialize concurrent first updates."""
    with data_lock(db, "backup", "daily"):
        db.daily_backup(verify_existing=False)

