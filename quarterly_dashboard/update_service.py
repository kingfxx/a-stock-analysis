"""Independent chip facts, idempotent legacy import and incremental financing."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from .chips import _fetch_chip_report, _number, chip_rows, financing_window_start
from .network import create_data_session
from .financing_storage import effective_fields, NUMERIC_FIELDS
from .sources import normalize_code
from .storage import Database, SyncKey, SyncResult, utc_now

FINANCING_SOURCE = "eastmoney:RPTA_WEB_RZRQ_GGMX"
HOLDER_SOURCES = ("RPT_F10_EH_HOLDERNUM", "RPT_HOLDERNUM_DET")
_LOCKS, _LOCKS_GUARD = {}, Lock()


class FinancingHistoryGap(ValueError):
    """A response omitted already stored dates in the requested overlap."""


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


def checked_today(state):
    """Daily source checks follow the Shanghai calendar, not a rolling TTL."""
    checked = state.get("checked_at")
    if not checked:
        return False
    local = timezone(timedelta(hours=8))
    stamp = datetime.fromisoformat(checked.replace("Z", "+00:00"))
    return stamp.astimezone(local).date() == datetime.now(local).date()


AUDIT_DAYS = {"financial": 180, "valuation": 90, "prices_adjusted": 30}


def audit_due(state, dataset="prices_adjusted"):
    if dataset.split(":", 1)[0] not in AUDIT_DAYS:
        return False
    return not state.get("next_full_audit_at") or state["next_full_audit_at"] <= utc_now()


def next_audit(dataset="prices_adjusted"):
    days = AUDIT_DAYS.get(dataset.split(":", 1)[0])
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days else None


def mark_failed(db, run, exc):
    with db.connection() as conn:
        row = conn.execute("SELECT status FROM sync_runs WHERE id=?", (run,)).fetchone()
    if row and row[0] == "running":
        db.fail_sync(run, str(exc))


def backup_before_update(db):
    """Keep the first daily snapshot; serialize concurrent first updates."""
    with data_lock(db, "backup", "daily"):
        db.daily_backup(verify_existing=False)


class ChipService:
    def __init__(self, db: Database, legacy_root: Path, *, fetcher=None):
        self.db, self.legacy_root, self.fetcher = db, Path(legacy_root), fetcher or self._fetch

    @staticmethod
    def _fetch(code, section, source=None, **bounds):
        with create_data_session() as session:
            return _fetch_chip_report(code, section, session, source, **bounds)

    def _financing_candidates(self, code, records, old):
        chart_rows = chip_rows(records, "financing")
        rows, warnings = [], []
        fields = NUMERIC_FIELDS
        by_day = {r["DATE"][:10]: r for r in records}
        for item in chart_rows:
            raw = by_day[item["date"]]
            if raw.get("SCODE") != code:
                raise ValueError("融资记录股票不匹配")
            previous = old.get(item["date"], {})
            if previous:
                _, previous_retained, previous_fields = effective_fields(previous)
            else:
                previous_retained, previous_fields = {}, {}
            retained = {
                field: {"value": value, "run_id": previous_retained[field]["run_id"]
                        if field in previous_retained else previous["run_id"]}
                for field, value in previous_fields.items() if field not in raw
            }
            if retained:
                warnings.append("来源缺少字段，沿用旧值并保留来源记录")
            values = {}
            for source_field, column in fields.items():
                if source_field not in raw and source_field in previous_fields:
                    values[column] = previous[column]
                else:
                    value = _number(raw.get(source_field), 0 if column.endswith("balance") else None)
                    if column == "close" and value is not None and value <= 0:
                        raise ValueError("融资来源未复权价格无效")
                    if raw.get(source_field) is not None and value is None:
                        raise ValueError(f"融资字段无效：{source_field}")
                    values[column] = value  # Explicit null is unknown, not an implicit old value.
            rows.append(dict(date=item["date"], raw=raw, values=values, retained=retained))
        return rows, list(dict.fromkeys(warnings))

    def _save_financing(self, code, key, run, records, *, start=None, end=None, full=False, import_meta=None):
        with self.db.connection() as conn:
            bounds = (start or "0001-01-01", end or "9999-12-31")
            old = {r["trade_date"]: dict(r) for r in conn.execute(
                "SELECT * FROM financing_daily WHERE instrument_id=? AND source=? AND trade_date BETWEEN ? AND ?",
                (key.instrument_id, key.source, *bounds))}
            existing = conn.execute("SELECT min(trade_date),max(trade_date) FROM financing_daily "
                                    "WHERE instrument_id=? AND source=?", (key.instrument_id, key.source)).fetchone()
        rows, warnings = self._financing_candidates(code, records, old)
        fresh_dates = {r["date"] for r in rows}
        required = {d for d in old if (not start or d >= start) and (not end or d <= end)}
        if required - fresh_dates:
            raise FinancingHistoryGap(f"融资历史缺少已知日期 {len(required - fresh_dates)} 条")
        all_days = sorted(set(old) | fresh_dates | {d for d in existing if d})
        result = SyncResult(len(rows), all_days[0] if all_days else None, all_days[-1] if all_days else None,
                            all_days[-1] if all_days else None, no_data=not all_days,
                            next_full_audit_at=None,
                            checked_at=import_meta.get("checked_at") if import_meta else None)
        def write(conn):
            for row in rows:
                v = row["values"]
                conn.execute("INSERT INTO financing_daily(instrument_id,source,trade_date,margin_balance,short_balance,"
                             "total_balance,net_buy,close,raw_json,retained_fields_json,"
                             "content_hash,obtained_at,run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                             "ON CONFLICT(instrument_id,source,trade_date) DO UPDATE SET "
                             "margin_balance=excluded.margin_balance,short_balance=excluded.short_balance,"
                             "total_balance=excluded.total_balance,net_buy=excluded.net_buy,close=excluded.close,"
                             "raw_json=excluded.raw_json,retained_fields_json=excluded.retained_fields_json,"
                             "content_hash=excluded.content_hash,"
                             "obtained_at=excluded.obtained_at,run_id=excluded.run_id",
                             (key.instrument_id, key.source, row["date"], *(v[c] for c in (
                                 "margin_balance", "short_balance", "total_balance", "net_buy", "close")),
                              encoded(row["raw"]), encoded(row["retained"]),
                              digest(row["raw"]), utc_now(), run))
            if import_meta:
                self._import_record(conn, key, run, len(rows), import_meta)
        self.db.complete_sync(run, result, write)
        return warnings

    def _save_holders(self, code, key, run, records, import_meta=None, *, full=True):
        parsed = chip_rows(records, "shareholders")
        with self.db.connection() as conn:
            known = {r[0] for r in conn.execute("SELECT stat_date FROM shareholder_observations "
                                              "WHERE instrument_id=? AND source=?", (key.instrument_id, key.source))}
        if full and known - {r["date"] for r in parsed}:
            raise ValueError("股东历史缺少已知日期")
        days = sorted(known | {r["date"] for r in parsed})
        def write(conn):
            for raw, item in zip(sorted(records, key=lambda r: r["END_DATE"]), parsed):
                if raw.get("SECURITY_CODE") != code:
                    raise ValueError("股东记录股票不匹配")
                scope = "total" if "HOLDER_TOTAL_NUM" in raw else "unknown"
                record_key = item["date"] + ":" + scope
                conn.execute("INSERT INTO shareholder_observations(instrument_id,source,source_record_key,stat_date,"
                             "announced_on,holder_scope,holders,raw_json,content_hash,obtained_at,run_id) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument_id,source,source_record_key) "
                             "DO UPDATE SET announced_on=excluded.announced_on,holders=excluded.holders,"
                             "raw_json=excluded.raw_json,content_hash=excluded.content_hash,obtained_at=excluded.obtained_at,run_id=excluded.run_id",
                             (key.instrument_id, key.source, record_key, item["date"], item["announced_on"], scope,
                              item["holders"], encoded(raw), digest(raw), utc_now(), run))
            if import_meta:
                self._import_record(conn, key, run, len(parsed), import_meta)
        self.db.complete_sync(run, SyncResult(len(parsed), days[0] if days else None, days[-1] if days else None,
                                              days[-1] if days else None, no_data=not days, next_full_audit_at=None,
                                              checked_at=import_meta.get("checked_at") if import_meta else None), write)

    @staticmethod
    def _import_record(conn, key, run, count, meta):
        conn.execute("INSERT INTO legacy_imports VALUES (?,?,?,?,?,?,?)",
                     (meta["path"], meta["hash"], key.dataset, key.instrument_id, run, count, utc_now()))

    def import_legacy(self, code, section):
        path = (self.legacy_root / section / f"{normalize_code(code)}.json").resolve()
        if not path.exists():
            return False
        with data_lock(self.db, "import:" + section, code):
            with self.db.connection() as conn:
                if conn.execute("SELECT 1 FROM legacy_imports WHERE path=? AND dataset=?",
                                (str(path), section)).fetchone():
                    return False
            contents = path.read_bytes()
            data = json.loads(contents)
            if data.get("code") != code or not isinstance(data.get("records"), list):
                raise ValueError("旧筹码缓存结构/股票不正确")
            if not data["records"] and data.get("empty") is not True:
                raise ValueError("旧缓存没有明确的无数据状态")
            checked = data.get("updated_on")
            stamp = datetime.combine(date.fromisoformat(checked), datetime.min.time(), timezone.utc).isoformat() if checked else None
            sha = hashlib.sha256(contents).hexdigest()
            identity = self.db.ensure_instrument(code)
            source = FINANCING_SOURCE if section == "financing" else "legacy:shareholders"
            key = SyncKey(identity, section, source)
            state = sync_state(self.db, key)
            if state.get("data_status") in {"data", "no_data"}:
                return False  # The database is already authoritative, never replay an old file.
            archive = path.with_name(path.name + f".migration-{sha[:12]}.bak")
            if not archive.exists():
                with archive.open("xb") as handle:
                    handle.write(contents)
            elif archive.read_bytes() != contents:
                raise ValueError("迁移备份内容冲突")
            backup = self.db.path.parent / "backups" / f"pre-import-{section}-{code}-{sha[:12]}.sqlite3"
            if not backup.exists():
                self.db.backup(backup)
            run = self.db.start_sync(key, parser_version="legacy-json-v1", methodology_version="legacy-facts",
                                     trigger_reason="legacy_import")
            try:
                if path.read_bytes() != contents:
                    raise ValueError("迁移期间原文件发生变化")
                meta = {"path": str(path), "hash": sha, "checked_at": stamp or "1970-01-01T00:00:00+00:00"}
                if section == "financing":
                    self._save_financing(code, key, run, data["records"], full=True, import_meta=meta)
                else:
                    self._save_holders(code, key, run, data["records"], import_meta=meta)
            except Exception as exc:
                mark_failed(self.db, run, exc)
                raise
            return True

    def update(self, code, section, *, refresh=False, full=False, today=None):
        code, today = normalize_code(code), today or date.today()
        backup_before_update(self.db)
        warnings = []
        with data_lock(self.db, section, code):
            try:
                self.import_legacy(code, section)
            except (ValueError, OSError) as exc:
                warnings.append(f"旧缓存导入失败，原文件保留：{exc}")
            identity = self.db.ensure_instrument(code)
            sources = (FINANCING_SOURCE,) if section == "financing" else tuple("eastmoney:" + s for s in HOLDER_SOURCES)
            for source in sources:
                key = SyncKey(identity, section, source)
                state = sync_state(self.db, key)
                if not refresh and not full and checked_today(state):
                    continue
                complete = full or (not state.get("data_watermark") if section == "financing"
                                   else state.get("data_status") not in {"data", "no_data"})
                bounds = {}
                if section == "financing":
                    start = None if complete else (date.fromisoformat(state["data_watermark"]) - timedelta(days=14)).isoformat()
                else:
                    checked = datetime.fromisoformat(state["checked_at"]) if state.get("checked_at") else None
                    start = None if complete else (checked.date() - timedelta(days=30)).isoformat()
                    if not complete:
                        bounds["date_field"] = "NOTICE_DATE" if source.endswith("RPT_F10_EH_HOLDERNUM") else "HOLD_NOTICE_DATE"
                end = today.isoformat()
                run = self.db.start_sync(key, parser_version="eastmoney-facts-v1", methodology_version="source-fields-v1",
                                         trigger_reason="full_audit" if complete else "incremental",
                                         requested_start=start, requested_end=end)
                try:
                    records = self.fetcher(code, section, source.split(":", 1)[1], start_date=start, end_date=end, **bounds)
                    if section == "financing":
                        try:
                            warnings += self._save_financing(code, key, run, records, start=start, end=end, full=complete)
                        except FinancingHistoryGap:
                            if complete:
                                raise
                            records = self.fetcher(code, section, source.split(":", 1)[1], start_date=None, end_date=end)
                            with self.db.connection(write=True) as conn:
                                conn.execute("UPDATE sync_runs SET trigger_reason='historical_gap',requested_start=NULL WHERE id=?", (run,))
                            warnings += self._save_financing(code, key, run, records, end=end, full=True)
                    else:
                        self._save_holders(code, key, run, records, full=complete)
                except Exception as exc:
                    mark_failed(self.db, run, exc)
                    warnings.append(f"{source} 更新失败，保留已有记录：{exc}")
        result = self.cached(code, section, today=today)
        result["warnings"] = list(dict.fromkeys(warnings))
        return result

    def cached(self, code, section, *, today=None):
        today = today or date.today()
        identity = instrument_id(self.db, code)
        result = dict(rows=[], stored_count=0, updated_on=None, warnings=[], empty=False)
        if identity is None:
            return result
        with self.db.connection() as conn:
            states = [dict(r) for r in conn.execute("SELECT * FROM sync_state WHERE instrument_id=? AND dataset=?",
                                                  (identity, section))]
            result["updated_on"] = max((s["checked_at"][:10] for s in states if s["checked_at"]), default=None)
            live_sources = (FINANCING_SOURCE,) if section == "financing" else tuple("eastmoney:" + s for s in HOLDER_SOURCES)
            live_states = {s["source"]: s for s in states if s["source"] in live_sources}
            result["needs_update"] = any(not checked_today(live_states.get(source, {})) for source in live_sources)
            result["empty"] = bool(states) and all(s["data_status"] == "no_data" for s in states)
            if section == "financing":
                start, end = financing_window_start(today), today.isoformat()
                result.update(window_start=start, window_end=end)
                result["stored_count"] = conn.execute("SELECT count(*) FROM financing_daily WHERE instrument_id=?", (identity,)).fetchone()[0]
                result["rows"] = [dict(date=r["trade_date"], margin_balance=r["margin_balance"], total_balance=r["total_balance"],
                                       net_buy=r["net_buy"], close=r["close"], raw_close=r["close"], price_date=r["trade_date"])
                                  for r in conn.execute("SELECT * FROM financing_daily WHERE instrument_id=? "
                                                        "AND trade_date BETWEEN ? AND ? ORDER BY trade_date", (identity, start, end))]
                latest = conn.execute("SELECT * FROM financing_daily WHERE instrument_id=? AND trade_date<=? "
                                      "ORDER BY trade_date DESC LIMIT 1", (identity, end)).fetchone()
                result["latest"] = dict(date=latest["trade_date"], margin_balance=latest["margin_balance"], total_balance=latest["total_balance"], net_buy=latest["net_buy"]) if latest else None
            else:
                series = {}
                for r in conn.execute("SELECT * FROM shareholder_observations WHERE instrument_id=? ORDER BY stat_date", (identity,)):
                    key = r["source"] + "|" + r["holder_scope"]
                    item = series.setdefault(key, dict(id=key, source=r["source"], scope=r["holder_scope"], rows=[]))
                    item["rows"].append(dict(date=r["stat_date"], holders=r["holders"], announced_on=r["announced_on"], close=None, price_date=None))
                ordered = sorted(series.values(), key=lambda s: (s["scope"] != "total", s["source"].startswith("legacy:"), s["id"]))
                result["series"] = ordered
                result["rows"] = ordered[0]["rows"] if ordered else []
                result["stored_count"] = sum(len(s["rows"]) for s in ordered)
                result["latest"] = next((r for r in reversed(result["rows"]) if r["date"] <= today.isoformat()), None)
        return result
