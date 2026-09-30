"""Shared daily prices with checked immutable qfq generations and bounded leases."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from .network import create_data_session
from .sources import fetch_price_history, normalize_code, price_equal
from .storage import Database, SyncKey, SyncResult, StorageError, utc_now
from .update_service import (audit_due, data_lock, digest, encoded, instrument_id, mark_failed,
                             next_audit, recently_checked, sync_state, backup_after_update)

SOURCE = "tencent"


class PriceVersionUnavailable(StorageError):
    pass


def _stored_price(row):
    raw = json.loads(row["raw_json"])
    values = dict(date=row["trade_date"], close=row["close"], raw=raw)
    if isinstance(raw, list) and len(raw) >= 6:
        values.update(open=float(raw[1]), high=float(raw[3]), low=float(raw[4]), volume=float(raw[5]))
    return values


class PriceService:
    def __init__(self, db: Database, *, fetcher=None, anchor_count=2):
        self.db, self.fetcher, self.anchor_count = db, fetcher or self._fetch, anchor_count

    @staticmethod
    def _fetch(code, adjustment, start, end):
        with create_data_session() as session:
            return fetch_price_history(code, session, "" if adjustment == "raw" else adjustment, start, end)

    def current_version(self, code):
        identity = instrument_id(self.db, code)
        if identity is None:
            return None
        return sync_state(self.db, SyncKey(identity, "prices_adjusted", SOURCE, "qfq")).get("active_price_version_id")

    def version_info(self, code, version):
        with self.db.connection() as conn:
            row = conn.execute("SELECT v.id,v.validated_at,v.coverage_start,v.coverage_end,v.source_basis "
                               "FROM adjusted_price_versions v JOIN instruments i ON i.id=v.instrument_id "
                               "WHERE v.id=? AND i.code=? AND v.source=? AND v.status='complete'",
                               (version, normalize_code(code), SOURCE)).fetchone()
            return dict(row) if row else {}

    def read(self, code, adjustment="qfq", *, version=None, start="1990-01-01", end="9999-12-31", lease=True):
        code = normalize_code(code)
        identity = instrument_id(self.db, code)
        if identity is None:
            if version:
                raise PriceVersionUnavailable("前复权版本不存在，请重新加载价格")
            return []
        if adjustment == "raw":
            with self.db.connection() as conn:
                return [_stored_price(r) for r in conn.execute("SELECT * FROM raw_daily_prices WHERE "
                    "instrument_id=? AND source=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date", (identity, SOURCE, start, end))]
        if adjustment != "qfq":
            raise ValueError("不支持的价格口径")
        version = self.current_version(code) if version is None else version
        if not version:
            return []
        with self.db.connection(write=lease) as conn:
            meta = conn.execute("SELECT * FROM adjusted_price_versions WHERE id=? AND instrument_id=? "
                                "AND source=? AND adjustment='qfq' AND status='complete'", (version, identity, SOURCE)).fetchone()
            if not meta:
                raise PriceVersionUnavailable("前复权版本已过期，请重新获取价格上下文")
            if lease:
                conn.execute("INSERT INTO price_version_leases VALUES (?,?) ON CONFLICT(version_id) "
                             "DO UPDATE SET expires_at=excluded.expires_at",
                             (version, (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()))
            return [_stored_price(r) for r in conn.execute("SELECT * FROM adjusted_daily_prices WHERE version_id=? "
                                                           "AND trade_date BETWEEN ? AND ? ORDER BY trade_date", (version, start, end))]

    @staticmethod
    def _validate(rows, start, end):
        seen = set()
        for row in rows:
            day = date.fromisoformat(row["date"]).isoformat()
            if day in seen or not start <= day <= end:
                raise ValueError("价格候选日期重复或超出请求范围")
            if any(k not in row for k in ("open", "close", "high", "low", "volume", "raw")):
                raise ValueError("价格候选缺少完整来源字段")
            encoded(row)  # Reject NaN/infinite payloads before writing.
            seen.add(day)
        if not rows:
            raise ValueError("价格候选为空")
        if [r["date"] for r in rows] != sorted(seen):
            raise ValueError("价格候选未按交易日期排序")

    def ensure(self, code, adjustment="qfq", *, refresh=False, full=False, today=None):
        code, today = normalize_code(code), today or date.today()
        requested_at = utc_now()
        with data_lock(self.db, "prices:" + adjustment, code):
            identity = self.db.ensure_instrument(code)
            key = SyncKey(identity, "prices_raw" if adjustment == "raw" else "prices_adjusted", SOURCE, adjustment)
            state = sync_state(self.db, key)
            with self.db.connection() as conn:
                prior_failure = conn.execute("SELECT error,finished_at FROM sync_runs WHERE instrument_id=? "
                                             "AND dataset=? AND source=? AND adjustment=? AND status='failed' "
                                             "AND finished_at>=? ORDER BY id DESC LIMIT 1", (*key.values(), requested_at)).fetchone()
            if prior_failure:
                message = f"价格更新失败，继续使用已有版本：{prior_failure['error']}"
                if state.get("data_status") == "data":
                    return dict(version=state.get("active_price_version_id"), warnings=[message], changed=False)
                raise ValueError(f"{code} {adjustment} 价格加载失败：{prior_failure['error']}")
            # A full audit already in flight satisfies followers, while a later
            # explicit full audit still runs even inside the ordinary cache TTL.
            if full and (state.get("succeeded_at") or "") >= requested_at:
                with self.db.connection() as conn:
                    prior = conn.execute("SELECT requested_start FROM sync_runs WHERE id=?",
                                         (state["last_success_run_id"],)).fetchone()
                if prior and prior[0] == "1990-01-01":
                    return dict(version=state.get("active_price_version_id"), warnings=[], changed=False)
            if not full and recently_checked(state, 5 if refresh else 600):
                return dict(version=state.get("active_price_version_id"), warnings=[], changed=False)
            old = self.read(code, adjustment, lease=False)
            old_by_day = {r["date"]: r for r in old}
            rebuild = full or not old or audit_due(state)
            start = "1990-01-01" if rebuild else old[max(0, len(old) - (30 if adjustment == "raw" else 20))]["date"]
            end = today.isoformat()
            run = self.db.start_sync(key, parser_version="tencent-ohlc-v1", methodology_version="current-adjustment-v1",
                                     trigger_reason="full_audit" if rebuild else "overlap_check",
                                     requested_start=start, requested_end=end)
            try:
                rows = self.fetcher(code, adjustment, start, end)
                self._validate(rows, start, end)
                if not rebuild:
                    overlap = [r for r in rows if r["date"] in old_by_day]
                    expected = {d for d in old_by_day if d >= start}
                    missing = expected - {r["date"] for r in rows}
                    if missing and adjustment == "raw":
                        raise ValueError("价格增量响应缺少已知交易日期")
                    if adjustment == "qfq":
                        rebuild = bool(missing) or len(overlap) < min(20, len(old)) or any(not price_equal(r, old_by_day[r["date"]]) for r in overlap)
                        # Two older anchors cost one window request each, never a hidden full download.
                        anchors = [old[0], old[len(old) // 2]][:self.anchor_count] if len(old) > 40 else []
                        for anchor in anchors:
                            if rebuild:
                                break
                            sample = self.fetcher(code, adjustment, anchor["date"], anchor["date"])
                            self._validate(sample, anchor["date"], anchor["date"])
                            rebuild = len(sample) != 1 or not price_equal(anchor, sample[0])
                        if rebuild:
                            rows = self.fetcher(code, adjustment, "1990-01-01", end)
                            self._validate(rows, "1990-01-01", end)
                            with self.db.connection(write=True) as conn:
                                conn.execute("UPDATE sync_runs SET trigger_reason='historical_change',requested_start='1990-01-01' WHERE id=?", (run,))
                fresh = {r["date"]: r for r in rows}
                if rebuild and set(old_by_day) - set(fresh):
                    raise ValueError("完整价格重建遗漏已有历史")
                merged = fresh if rebuild else {**old_by_day, **fresh}
                ordered = [merged[d] for d in sorted(merged)]
                coverage = (ordered[0]["date"], ordered[-1]["date"], ordered[-1]["date"])
                next_full = next_audit() if rebuild else state.get("next_full_audit_at")
                changed = bool(set(merged) != set(old_by_day) or any(
                    d in old_by_day and not price_equal(r, old_by_day[d]) for d, r in fresh.items()))
                warnings = []
                if adjustment == "raw":
                    def write(conn):
                        conn.executemany("INSERT INTO raw_daily_prices(instrument_id,source,trade_date,open,close,high,low,volume,"
                                         "raw_json,content_hash,obtained_at,run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                                         "ON CONFLICT(instrument_id,source,trade_date) DO UPDATE SET open=excluded.open,close=excluded.close,"
                                         "high=excluded.high,low=excluded.low,volume=excluded.volume,raw_json=excluded.raw_json,"
                                         "content_hash=excluded.content_hash,obtained_at=excluded.obtained_at,run_id=excluded.run_id",
                                         [(identity, SOURCE, r["date"], r["open"], r["close"], r["high"], r["low"], r["volume"],
                                           encoded(r["raw"]), digest(r["raw"]), utc_now(), run) for r in rows])
                    self.db.complete_sync(run, SyncResult(len(rows), *coverage, next_full_audit_at=next_full), write)
                    version = None
                elif not changed and old:
                    version = state["active_price_version_id"]
                    self.db.complete_sync(run, SyncResult(0, *coverage, active_price_version_id=version,
                                                         next_full_audit_at=next_full), lambda conn: None)
                else:
                    # Stage candidates; only the final validated state is published atomically.
                    with self.db.connection(write=True) as conn:
                        version = conn.execute("INSERT INTO adjusted_price_versions(instrument_id,source,adjustment,source_basis,status,"
                                               "coverage_start,coverage_end,row_count,created_at,run_id) "
                                               "VALUES (?,?,'qfq','tencent-current-ohlc','candidate',?,?,?,?,?)",
                                               (identity, SOURCE, coverage[0], coverage[1], len(ordered), utc_now(), run)).lastrowid
                        if not rebuild and old:
                            conn.execute("INSERT INTO adjusted_daily_prices SELECT ?,trade_date,close,raw_json "
                                         "FROM adjusted_daily_prices WHERE version_id=?", (version, state["active_price_version_id"]))
                            inserts = [r for r in rows if r["date"] not in old_by_day]
                        else:
                            inserts = ordered
                        conn.executemany("INSERT INTO adjusted_daily_prices VALUES (?,?,?,?)",
                                         [(version, r["date"], r["close"], encoded(r["raw"])) for r in inserts])
                    def publish(conn):
                        conn.execute("UPDATE adjusted_price_versions SET status='complete',validated_at=? WHERE id=?", (utc_now(), version))
                    self.db.complete_sync(run, SyncResult(len(rows), *coverage, active_price_version_id=version,
                                                         next_full_audit_at=next_full), publish)
                    try:
                        self.prune(code)
                    except Exception as exc:
                        warnings.append(f"新价格版本已提交，旧版本清理失败：{exc}")
                return dict(version=version, warnings=warnings + backup_after_update(self.db), changed=changed)
            except Exception as exc:
                mark_failed(self.db, run, exc)
                with self.db.connection(write=True) as conn:
                    conn.execute("DELETE FROM adjusted_price_versions WHERE run_id=? AND status='candidate'", (run,))
                if old:
                    return dict(version=state.get("active_price_version_id"), warnings=[f"价格更新失败，继续使用已有版本：{exc}"], changed=False)
                raise ValueError(f"{code} {adjustment} 价格加载失败：{exc}") from exc

    def prune(self, code):
        identity = instrument_id(self.db, code)
        with self.db.connection(write=True) as conn:
            versions = [r[0] for r in conn.execute("SELECT id FROM adjusted_price_versions WHERE instrument_id=? AND source=? "
                                                  "AND status='complete' ORDER BY id DESC", (identity, SOURCE))]
            for version in versions[2:]:
                conn.execute("DELETE FROM adjusted_price_versions WHERE id=? AND NOT EXISTS "
                             "(SELECT 1 FROM sync_state WHERE active_price_version_id=?) AND NOT EXISTS "
                             "(SELECT 1 FROM price_version_leases WHERE version_id=? AND expires_at>?)",
                             (version, version, version, utc_now()))


def month_closes(prices):
    months = {}
    for row in prices:
        months[row["date"][:7]] = dict(date=row["date"], close=row["close"])
    return [months[m] for m in sorted(months)]
