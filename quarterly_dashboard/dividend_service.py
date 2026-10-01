"""Incremental dividend facts shared by the financial and yield projections."""

from __future__ import annotations

import json
import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .network import create_data_session
from .sources import normalize_code
from .storage import Database, SyncKey, SyncResult
from .update_service import (backup_before_update, data_lock, mark_failed,
                             recently_checked, sync_state)
from .valuation import fetch_dividend_event_page


SOURCE = "eastmoney:RPT_SHAREBONUS_DET"


def _source_day(value):
    token = str(value or "")[:10]
    return date.fromisoformat(token).isoformat() if token else None


class DividendService:
    def __init__(self, db: Database, legacy_root: Path, *, fetch_page=None):
        self.db = db
        self.legacy_root = Path(legacy_root)
        self.fetch_page = fetch_page or self._fetch_page

    @staticmethod
    def _fetch_page(code, *, date_field=None, since=None, report_period=None, page=1):
        with create_data_session() as session:
            return fetch_dividend_event_page(code, session, date_field=date_field, since=since,
                                             report_period=report_period, page=page)

    def _all_pages(self, code, *, date_field=None, since=None, report_period=None):
        page, records, total = 1, [], None
        while True:
            response = self.fetch_page(code, date_field=date_field, since=since,
                                       report_period=report_period, page=page)
            if total is not None and total != response["total"]:
                raise ValueError("分红分页总数变化")
            total = response["total"]
            records.extend(response["records"])
            if page >= response["pages"]:
                if len(records) != total:
                    raise ValueError("分红分页缺页")
                return records
            page += 1

    @staticmethod
    def _candidate(code, raw):
        if raw.get("SECURITY_CODE") != code:
            raise ValueError("分红记录股票不匹配")
        period = _source_day(raw.get("REPORT_DATE"))
        proposal = _source_day(raw.get("PLAN_NOTICE_DATE"))
        source_id = raw.get("EVENT_ID") or raw.get("ID")
        if not period or not proposal and not source_id:
            raise ValueError("分红记录缺少可消歧事件身份")
        key = str(source_id) if source_id else f"{period}|{proposal}"
        ex_date = _source_day(raw.get("EX_DIVIDEND_DATE"))
        status_text = str(raw.get("ASSIGN_PROGRESS") or "")
        if any(word in status_text for word in ("取消", "终止", "不分配")):
            status = "cancelled"
        elif ex_date and any(word in status_text for word in ("实施", "完成")):
            status = "implemented"
        else:
            status = "pending"
        amount = raw.get("PRETAX_BONUS_RMB")
        cash = float(amount) if amount not in (None, "", "--") else None
        if cash is not None and cash <= 0:
            cash = None
        return {"event_key": key, "source_event_id": str(source_id) if source_id else None,
                "report_period": period, "proposal_date": proposal,
                "notice_date": _source_day(raw.get("NOTICE_DATE")),
                "registration_date": _source_day(raw.get("EQUITY_RECORD_DATE")),
                "ex_dividend_date": ex_date, "status": status, "cash_per_ten": cash,
                "raw_json": raw}

    def _stored(self, code, source=SOURCE):
        with self.db.connection() as conn:
            identity = conn.execute("SELECT id FROM instruments WHERE code=?", (normalize_code(code),)).fetchone()
        return self.db.dividend_events(identity[0], source) if identity else []

    def read(self, code):
        real = self._stored(code)
        legacy = self._stored(code, "legacy")
        covered_periods = {row["report_period"] for row in real}
        rows = real + [row for row in legacy if row["report_period"] not in covered_periods]
        today = date.today().isoformat()
        events = []
        for row in rows:
            ex_date = row["ex_dividend_date"]
            if row["status"] != "implemented" or not ex_date or ex_date > today or not row["cash_per_ten"]:
                continue
            raw = json.loads(row["raw_json"])
            shares = raw.get("TOTAL_SHARES", raw.get("total_shares"))
            try:
                shares = float(shares) if shares is not None else None
            except (TypeError, ValueError):
                shares = None
            events.append({"date": ex_date, "report_period": row["report_period"],
                           "per_share": row["cash_per_ten"] / 10,
                           "total_shares": shares if shares and shares > 0 else None})
        return sorted(events, key=lambda row: row["date"])

    def import_legacy(self, code):
        code = normalize_code(code)
        path = self.legacy_root / f"{code}.json"
        if not path.exists():
            return False
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        document = json.loads(content.decode("utf-8"))
        if document.get("code") != code or not isinstance(document.get("dividend_events", []), list):
            raise ValueError("Legacy dividend cache has a mismatched code or event list")
        identity = self.db.ensure_instrument(code, document.get("name"))
        with self.db.connection() as conn:
            existing = conn.execute("SELECT content_hash FROM legacy_imports WHERE path=? AND dataset='dividends'",
                                    (str(path.resolve()),)).fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError("Legacy dividend cache changed after import; review both versions")
            return False
        rows = []
        for event in document.get("dividend_events", []):
            ex_date = _source_day(event.get("date"))
            period = _source_day(event.get("report_period"))
            amount = event.get("per_share")
            if not ex_date or amount is None or float(amount) <= 0:
                raise ValueError("Legacy dividend event is incomplete")
            rows.append({"event_key": f"legacy:{period}|{ex_date}", "source_event_id": None,
                         "report_period": period, "proposal_date": None, "notice_date": None,
                         "registration_date": None, "ex_dividend_date": ex_date,
                         "status": "implemented", "cash_per_ten": float(amount) * 10,
                         "raw_json": event})
        if len({row["event_key"] for row in rows}) != len(rows):
            raise ValueError("Legacy dividend events contain duplicate identities")
        anchors = sorted(row["report_period"] or row["ex_dividend_date"] for row in rows)
        key = SyncKey(identity, "dividends", "legacy")
        run = self.db.start_sync(key, parser_version="legacy_dividend_v1",
                                 methodology_version="legacy_implemented_v1", trigger_reason="legacy_import")
        try:
            result = SyncResult(len(rows), anchors[0] if anchors else None,
                                anchors[-1] if anchors else None, anchors[-1] if anchors else None,
                                no_data=not anchors)
            def write(conn):
                self.db.upsert_dividend_events(conn, key, run, rows)
                self.db.record_legacy_import(conn, key, run, str(path.resolve()), digest, len(rows))
            self.db.complete_sync(run, result, write)
        except Exception as exc:
            mark_failed(self.db, run, exc)
            raise
        return True

    def update(self, code, *, refresh=False, full=False):
        code = normalize_code(code)
        backup_before_update(self.db)
        with data_lock(self.db, "dividends", code):
            identity = self.db.ensure_instrument(code)
            key = SyncKey(identity, "dividends", SOURCE)
            state = sync_state(self.db, key)
            old = self.db.dividend_events(identity, SOURCE)
            known = bool(old) or state.get("data_status") == "no_data"
            if known and not (refresh or full
                            or not recently_checked(state, seconds=86400)):
                return {"events": self.read(code), "warnings": []}
            full_fetch = full or not known
            checked = state.get("checked_at")
            since = ((datetime.fromisoformat(checked.replace("Z", "+00:00")).date()
                      if checked else datetime.now(timezone.utc).date()) - timedelta(days=30)).isoformat()
            run = self.db.start_sync(key, parser_version="eastmoney_dividend_v1",
                                     methodology_version="implemented_cash_v1",
                                     trigger_reason="full" if full_fetch else "refresh",
                                     requested_start=None if full_fetch else since)
            warnings = []
            try:
                batches = [self._all_pages(code)] if full_fetch else [
                    self._all_pages(code, date_field="NOTICE_DATE", since=since),
                    self._all_pages(code, date_field="EX_DIVIDEND_DATE", since=since)]
                if not full_fetch:
                    for period in sorted({row["report_period"] for row in old if row["status"] == "pending"}):
                        try:
                            pending = self._all_pages(code, report_period=period)
                            if not pending:
                                raise ValueError("empty pending-event lookup")
                            batches.append(pending)
                        except ValueError:
                            batches.append(self._all_pages(code))
                            warnings.append("未完成事件精确查询不可用，已执行有记录原因的全量补偿")
                            break
                candidates = {}
                for batch in batches:
                    for raw in batch:
                        row = self._candidate(code, raw)
                        previous = candidates.get(row["event_key"])
                        if previous is not None and previous != row:
                            raise ValueError("分红窗口对同一事件返回冲突内容")
                        candidates[row["event_key"]] = row
                # A corrected proposal date changes the source business key. The
                # paid date and report period identify an unambiguous revision.
                renames = []
                old_keys = {row["event_key"] for row in old}
                for candidate in candidates.values():
                    if candidate["event_key"] in old_keys or not candidate["ex_dividend_date"]:
                        continue
                    matches = [row for row in old if row["event_key"] not in candidates
                               and row["report_period"] == candidate["report_period"]
                               and row["ex_dividend_date"] == candidate["ex_dividend_date"]]
                    if len(matches) > 1:
                        raise ValueError("分红身份修订无法消歧")
                    if len(matches) == 1:
                        if any(old_key == matches[0]["event_key"] for old_key, _ in renames):
                            raise ValueError("多个分红候选指向同一旧事件")
                        renames.append((matches[0]["event_key"], candidate["event_key"]))
                renamed_old = {old_key for old_key, _ in renames}
                if full_fetch and old and old_keys - candidates.keys() - renamed_old:
                    raise ValueError("分红全量响应漏掉已知事件；未执行删除")
                all_rows = {row["event_key"]: row for row in old if row["event_key"] not in renamed_old} | candidates
                anchors = sorted(row["report_period"] for row in all_rows.values() if row["report_period"])
                result = SyncResult(len(candidates), anchors[0] if anchors else None,
                                    anchors[-1] if anchors else None, anchors[-1] if anchors else None,
                                    no_data=not anchors,
                                    next_full_audit_at=None)
                def write(conn):
                    for old_key, new_key in renames:
                        conn.execute("UPDATE dividend_events SET event_key=? WHERE instrument_id=? "
                                     "AND source=? AND event_key=?", (new_key, identity, SOURCE, old_key))
                    self.db.upsert_dividend_events(conn, key, run, list(candidates.values()))
                self.db.complete_sync(run, result, write)
            except Exception as exc:
                mark_failed(self.db, run, exc)
                warnings.append(f"分红更新失败，保留已存事实：{exc}")
            return {"events": self.read(code), "warnings": warnings}
