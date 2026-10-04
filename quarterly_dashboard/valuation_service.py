"""Incremental Baidu observations, retaining the finest history actually obtained."""

from __future__ import annotations

import json
import hashlib
import math
from datetime import date, timedelta
from calendar import monthrange
from pathlib import Path

from .network import create_data_session
from .sources import normalize_code
from .storage import Database, SyncKey, SyncResult, utc_now
from .update_service import (audit_due, backup_before_update, data_lock, mark_failed,
                             next_audit, recently_checked, sync_state)
from .valuation import (fetch_industry_snapshot_details, fetch_valuation_indicator,
                        monthly_valuation, valuation_observation_rows)


SOURCE = "baidu:opendata"
INDUSTRY_SOURCE = "eastmoney:RPT_PCF10_INDUSTRY_CVALUE"
METRICS = ("pe", "pb", "market_cap")
INITIAL_WINDOWS = ("近十年", "近五年", "近三年")
SAMPLING_VERSION = "baidu_dense_observations_v1"


class ValuationService:
    def __init__(self, db: Database, legacy_root: Path, *, fetch_indicator=None, fetch_industry=None):
        self.db = db
        self.legacy_root = Path(legacy_root)
        self.fetch_indicator = fetch_indicator or self._fetch_indicator
        self.fetch_industry = fetch_industry

    @staticmethod
    def _fetch_indicator(code, metric, window):
        with create_data_session() as session:
            return fetch_valuation_indicator(code, session, metric, window=window)

    @staticmethod
    def _fetch_industry(code):
        with create_data_session() as session:
            return fetch_industry_snapshot_details(code, session)

    def _industry(self, identity):
        with self.db.connection() as conn:
            row = conn.execute("SELECT raw_json FROM industry_snapshots WHERE instrument_id=? AND source=? "
                               "ORDER BY snapshot_at DESC,id DESC LIMIT 1", (identity, INDUSTRY_SOURCE)).fetchone()
        if not row:
            return {}
        raw = json.loads(row[0])
        def positive(field):
            try:
                value = float(raw.get(field))
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) and value > 0 else None
        return {"pe": positive("PE_TTM"), "pb": positive("PB_MRQ"), "ps": positive("PS_TTM"),
                "peer_count": raw.get("TOTAL_COUNT"),
                "report_period": str(raw.get("REPORT_DATE") or "")[:10] or None}

    def _update_industry(self, code, identity, *, refresh=False, full=False):
        if self.fetch_industry is None:
            return [], False
        key = SyncKey(identity, "industry", INDUSTRY_SOURCE)
        state = sync_state(self.db, key)
        if state and not (refresh or full
                          or not recently_checked(state, seconds=86400)):
            return [], False
        run = self.db.start_sync(key, parser_version="eastmoney_peer_v1",
                                 methodology_version="current_peer_mean_v1",
                                 trigger_reason="refresh")
        try:
            details = self.fetch_industry(code)
            raw = details["raw"]
            with self.db.connection() as conn:
                latest = conn.execute("SELECT raw_json FROM industry_snapshots WHERE instrument_id=? AND source=? "
                                      "ORDER BY snapshot_at DESC,id DESC LIMIT 1",
                                      (identity, INDUSTRY_SOURCE)).fetchone()
            changed = latest is None or json.loads(latest[0]) != raw
            today = date.today().isoformat()
            result = SyncResult(1 if changed else 0, state.get("coverage_start") or today,
                                today, today, next_full_audit_at=None)
            def write(conn):
                if changed:
                    self.db.insert_industry_snapshot(conn, key, run, {
                        "snapshot_at": utc_now(), "industry_name": details["industry_name"],
                        "industry_code": details.get("industry_code"),
                        "classification_basis": "eastmoney_current_peer_group_v1", "raw_json": raw})
            self.db.complete_sync(run, result, write)
            return [], True
        except Exception as exc:
            mark_failed(self.db, run, exc)
            return [f"行业估值更新失败，保留已存快照：{exc}"], False

    def observations(self, code, metric):
        code = normalize_code(code)
        if metric not in METRICS:
            raise ValueError("Unknown valuation metric")
        with self.db.connection() as conn:
            instrument = conn.execute("SELECT id FROM instruments WHERE code=?", (code,)).fetchone()
        return self.db.valuation_observations(instrument[0], SOURCE, metric) if instrument else []

    @staticmethod
    def _collect(code, metric, windows, fetch):
        merged = {}
        for window in windows:
            points = fetch(code, metric, window)
            if not points:
                raise ValueError(f"百度{metric} {window}返回空序列")
            seen = set()
            for point in points:
                source_window = point.get("source_window", window)
                day = date.fromisoformat(point["date"]).isoformat()
                if day in seen:
                    raise ValueError(f"百度{metric} {window}重复日期 {day}")
                seen.add(day)
                value = float(point["value"])
                if not math.isfinite(value):
                    raise ValueError(f"百度{metric} {window}非有限数值")
                prior = merged.get(day)
                if prior and abs(prior["value"] - value) > 1e-8:
                    raise ValueError(f"百度{metric}跨窗口同日不一致 {day}")
                if prior:
                    if source_window not in prior["source_windows"]:
                        prior["source_windows"].append(source_window)
                else:
                    merged[day] = {"observed_on": day, "value": value,
                                   "source_windows": [source_window], "sampling_version": SAMPLING_VERSION,
                                   "raw_json": point}
        return [merged[day] for day in sorted(merged)]

    def read(self, code, reports):
        facts = {metric: self.observations(code, metric) for metric in METRICS}
        series = {metric: [{"date": row["observed_on"], "value": row["value"]}
                           for row in facts[metric]] for metric in METRICS}
        coverage = {}
        for metric, records in facts.items():
            days = [date.fromisoformat(row["observed_on"]) for row in records]
            windows = {}
            for row in records:
                for window in json.loads(row["source_windows_json"]):
                    windows.setdefault(window, []).append(row["observed_on"])
            coverage[metric] = {"start": days[0].isoformat() if days else None,
                                "end": days[-1].isoformat() if days else None,
                                "count": len(days),
                                "max_gap_days": max(((right - left).days for left, right in zip(days, days[1:])),
                                                    default=0),
                                "windows": {name: {"start": min(values), "end": max(values),
                                                   "count": len(values)} for name, values in windows.items()}}
        observation_rows = valuation_observation_rows(series, reports)
        rows = monthly_valuation(series, reports) if any(series.values()) else []
        with self.db.connection() as conn:
            instrument = conn.execute("SELECT id FROM instruments WHERE code=?", (normalize_code(code),)).fetchone()
            snapshots = ([dict(row) for row in conn.execute(
                "SELECT * FROM legacy_valuation_snapshots WHERE instrument_id=? ORDER BY observed_month",
                (instrument[0],))] if instrument else [])
        if not rows and snapshots:
            source = [json.loads(snapshot["row_json"]) for snapshot in snapshots]
            return {"rows": [item["row"] for item in source], "observation_rows": [], "coverage": coverage,
                    "series": series, "industry": self._industry(instrument[0]) or source[-1].get("industry", {}),
                    "updated_on": None, "basis": snapshots[-1]["methodology_version"],
                    "legacy_snapshot_available": True,
                    "warnings": ["当前显示旧月度估值快照，尚未取得新口径原始观察。"]}
        checked = [sync_state(self.db, SyncKey(instrument[0], f"valuation:{metric}", SOURCE)).get("checked_at")
                   for metric in METRICS] if instrument else []
        updated_on = min(value[:10] for value in checked) if checked and all(checked) else None
        return {"rows": rows, "observation_rows": observation_rows, "series": series, "coverage": coverage,
                "industry": self._industry(instrument[0]) if instrument else {},
                "updated_on": updated_on, "basis": SAMPLING_VERSION,
                "legacy_snapshot_available": bool(snapshots), "warnings": []}

    def import_legacy(self, code):
        code = normalize_code(code)
        path = self.legacy_root / f"{code}.json"
        if not path.exists():
            return False
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        document = json.loads(content.decode("utf-8"))
        if not isinstance(document.get("rows"), list):
            raise ValueError("Legacy valuation cache has no monthly rows")
        identity = self.db.ensure_instrument(code)
        with self.db.connection() as conn:
            existing = conn.execute("SELECT content_hash FROM legacy_imports WHERE path=? AND dataset='valuation_legacy'",
                                    (str(path.resolve()),)).fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError("Legacy valuation cache changed after import; review both versions")
            return False
        by_month = {}
        for row in document["rows"]:
            observed = date.fromisoformat(row["date"]).isoformat()
            month = observed[:7]
            if month in by_month:
                raise ValueError("Legacy valuation cache has duplicate months")
            by_month[month] = {"row": row, "industry": document.get("industry", {})}
        anchors = sorted(item["row"]["date"] for item in by_month.values())
        key = SyncKey(identity, "valuation_legacy", "legacy")
        run = self.db.start_sync(key, parser_version="legacy_valuation_v1",
                                 methodology_version=document.get("basis") or "legacy_monthly_unknown",
                                 trigger_reason="legacy_import")
        try:
            result = SyncResult(len(by_month), anchors[0] if anchors else None,
                                anchors[-1] if anchors else None, anchors[-1] if anchors else None,
                                no_data=not anchors)
            def write(conn):
                for month, item in by_month.items():
                    self.db.insert_legacy_valuation_snapshot(conn, key, run, str(path.resolve()), month,
                        document.get("basis") or "legacy_monthly_unknown", item)
                self.db.record_legacy_import(conn, key, run, str(path.resolve()), digest, len(by_month))
            self.db.complete_sync(run, result, write)
        except Exception as exc:
            mark_failed(self.db, run, exc)
            raise
        return True

    def update(self, code, reports, *, refresh=False, full=False):
        code = normalize_code(code)
        backup_before_update(self.db)
        with data_lock(self.db, "valuation", code):
            identity = self.db.ensure_instrument(code)
            warnings = []
            for metric in METRICS:
                key = SyncKey(identity, f"valuation:{metric}", SOURCE)
                state = sync_state(self.db, key)
                saved = self.db.valuation_observations(identity, SOURCE, metric)
                if saved and not (refresh or full or audit_due(state, "valuation")
                                  or not recently_checked(state, seconds=86400)):
                    continue
                full_metric = full or not saved or audit_due(state, "valuation")
                windows = INITIAL_WINDOWS if full_metric else ("近一年",)
                if not full_metric and saved:
                    last = date.fromisoformat(saved[-1]["observed_on"])
                    gap = date.today() - last
                    if gap > timedelta(days=365 * 5):
                        windows = ("近十年",)
                    elif gap > timedelta(days=365 * 3):
                        windows = ("近五年",)
                    elif gap > timedelta(days=365):
                        windows = ("近三年",)
                run = self.db.start_sync(key, parser_version="baidu_observations_v1",
                                         methodology_version=SAMPLING_VERSION,
                                         trigger_reason="full" if full_metric else "refresh")
                try:
                    candidates = self._collect(code, metric, windows, self.fetch_indicator)
                    if saved and not full_metric:
                        saved_days = {row["observed_on"] for row in saved}
                        candidate_days = {row["observed_on"] for row in candidates}
                        for wider in ("近三年", "近五年", "近十年"):
                            if saved_days & candidate_days or wider in windows:
                                continue
                            candidates = self._collect(code, metric, (wider,), self.fetch_indicator)
                            candidate_days = {row["observed_on"] for row in candidates}
                        if not saved_days & candidate_days:
                            raise ValueError("估值窗口与已保存尾部没有共同观察日，未标记连续覆盖")
                    if saved and not full_metric:
                        today = date.today()
                        month_index = today.year * 12 + today.month - 1 - 3
                        year, month = divmod(month_index, 12)
                        recent_start = date(year, month + 1, min(today.day, monthrange(year, month + 1)[1]))
                        cutoff = min(recent_start.isoformat(), saved[-1]["observed_on"])
                        candidates = [row for row in candidates if row["observed_on"] >= cutoff]
                        required = {row["observed_on"] for row in saved if row["observed_on"] >= cutoff}
                        if not candidates or required - {row["observed_on"] for row in candidates}:
                            raise ValueError("估值增量响应缺少回看范围内已知观察日，保留旧事实")
                    all_days = sorted({r["observed_on"] for r in saved} |
                                      {r["observed_on"] for r in candidates})
                    result = SyncResult(len(candidates), all_days[0], all_days[-1], all_days[-1],
                                        next_full_audit_at=next_audit("valuation") if full_metric else state.get("next_full_audit_at"))
                    # Retain provenance when a narrower window observes an existing date.
                    old = {r["observed_on"]: r for r in saved}
                    for row in candidates:
                        previous = old.get(row["observed_on"])
                        if previous:
                            windows_seen = json.loads(previous["source_windows_json"])
                            row["source_windows"] = list(dict.fromkeys(windows_seen + row["source_windows"]))
                    self.db.complete_sync(run, result,
                        lambda conn: self.db.upsert_valuation_observations(conn, key, run, candidates))
                except Exception as exc:
                    mark_failed(self.db, run, exc)
                    warnings.append(f"{metric} 估值更新失败，保留已存事实：{exc}")
            industry_warnings, _ = self._update_industry(
                code, identity, refresh=refresh, full=full)
            warnings.extend(industry_warnings)
            result = self.read(code, reports)
            result["warnings"] = warnings
            return result
