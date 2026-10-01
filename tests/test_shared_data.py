import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from datetime import date, timedelta
from pathlib import Path

import pytest

from quarterly_dashboard import server
from quarterly_dashboard.chips import _fetch_chip_report
from quarterly_dashboard.price_projection import chip_prices, financial_prices, valuation_prices
from quarterly_dashboard.price_service import PriceService, PriceVersionUnavailable
from quarterly_dashboard.sources import fetch_price_history, parse_price_history
from quarterly_dashboard.storage import Database, SyncKey, SyncResult, instance_lock
from quarterly_dashboard.update_service import ChipService, FINANCING_SOURCE, sync_state


@pytest.fixture
def db(tmp_path):
    value = Database(tmp_path / "shared.sqlite3")
    with instance_lock(value.path):
        value.initialize()
    return value


def margin(day, balance=100):
    return dict(SCODE="600887", DATE=day, RZYE=balance, RQYE=3, RZRQYE=103,
                RZJME=-10, SPJ=20, RQMCL=67900)


def holder(day, count=100, *, total=True):
    return {"SECURITY_CODE": "600887", "END_DATE": day, "NOTICE_DATE": "2026-09-01",
            "HOLDER_TOTAL_NUM" if total else "HOLDER_NUM": count}


def expire(db):
    with db.connection(write=True) as conn:
        conn.execute("UPDATE sync_state SET checked_at='2000-01-01T00:00:00+00:00'")


def price(day, value=10):
    return dict(date=day, open=value, close=value + .01, high=value + 1, low=value - 1,
                volume=100, raw=[day, str(value), str(value + .01), str(value + 1), str(value - 1), "100"])


def history(count=60):
    days = []
    current = date(2026, 6, 1)
    while len(days) < count:
        if current.weekday() < 5:
            days.append(price(current.isoformat(), 10 + len(days)))
        current += timedelta(days=1)
    return days


class Prices:
    def __init__(self, rows):
        self.rows, self.calls = copy.deepcopy(rows), []
    def __call__(self, code, adjustment, start, end):
        self.calls.append((adjustment, start, end))
        return copy.deepcopy([r for r in self.rows if start <= r["date"] <= end])


def test_financing_first_full_then_incremental_keeps_old_history_and_revision(db, tmp_path):
    rows = [margin("2020-01-02"), margin("2026-09-28"), margin("2026-09-29")]
    calls = []
    def fetch(code, section, source, **bounds):
        calls.append(bounds)
        return copy.deepcopy([r for r in rows if not bounds["start_date"] or r["DATE"] >= bounds["start_date"]])
    service = ChipService(db, tmp_path / "chips", fetcher=fetch)
    assert service.update("600887", "financing")["stored_count"] == 3
    rows[1]["RZYE"] = 90
    rows.append(margin("2026-09-30", 120))
    data = service.update("600887", "financing", refresh=True)
    assert data["stored_count"] == 4 and len(data["rows"]) == 3
    assert calls[0]["start_date"] is None
    assert calls[1]["start_date"] == "2026-09-15"
    assert data["rows"][0]["margin_balance"] == 90
    with db.connection() as conn:
        assert conn.execute("SELECT margin_balance FROM financing_daily WHERE trade_date='2020-01-02'").fetchone()[0] == 100
    assert db.check()["integrity"] == "ok"


def test_financing_cached_returns_five_calendar_years_without_trimming_storage(db, tmp_path):
    rows = [margin(day) for day in ("2021-09-30", "2021-10-01", "2023-10-01", "2025-10-01", "2026-10-01")]
    service = ChipService(db, tmp_path, fetcher=lambda *a, **kw: copy.deepcopy(rows))
    data = service.update("600887", "financing", today=date(2026, 10, 1))
    assert data["window_start"] == "2021-10-01"
    assert data["window_end"] == "2026-10-01"
    assert data["stored_count"] == 5
    assert [row["date"] for row in data["rows"]] == ["2021-10-01", "2023-10-01", "2025-10-01", "2026-10-01"]
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM financing_daily").fetchone()[0] == 5


def test_financing_five_year_window_handles_leap_day(db, tmp_path):
    service = ChipService(db, tmp_path, fetcher=lambda *a, **kw: [margin("2024-02-29")])
    data = service.update("600887", "financing", today=date(2024, 2, 29))
    assert data["window_start"] == "2019-02-28"


def test_financing_missing_optional_and_explicit_null_have_distinct_provenance(db, tmp_path):
    rows = [margin("2026-09-29")]
    service = ChipService(db, tmp_path, fetcher=lambda *a, **kw: copy.deepcopy(rows))
    service.update("600887", "financing")
    del rows[0]["RQMCL"]
    del rows[0]["RZRQYE"]
    data = service.update("600887", "financing", refresh=True)
    assert data["warnings"]
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM financing_daily").fetchone()
        assert "RQMCL" not in json.loads(row["raw_json"])
        assert json.loads(row["retained_fields_json"])["RQMCL"]["value"] == 67900
        assert json.loads(row["retained_fields_json"])["RQMCL"]["run_id"] != row["run_id"]
        assert row["total_balance"] == 103
    rows[0]["RZRQYE"] = None
    service.update("600887", "financing", refresh=True)
    with db.connection() as conn:
        assert conn.execute("SELECT total_balance FROM financing_daily").fetchone()[0] is None


@pytest.mark.parametrize("bad", ["empty", "missing_date", "required", "wrong_stock", "duplicate"])
def test_bad_financing_batch_preserves_data_and_watermark(db, tmp_path, bad):
    rows = [margin("2026-09-28"), margin("2026-09-29")]
    service = ChipService(db, tmp_path, fetcher=lambda *a, **kw: copy.deepcopy(rows))
    service.update("600887", "financing")
    key = SyncKey(db.ensure_instrument("600887"), "financing", FINANCING_SOURCE)
    original = sync_state(db, key)
    if bad == "empty": rows.clear()
    elif bad == "missing_date": rows.pop()
    elif bad == "required": rows[1]["RZYE"] = None
    elif bad == "wrong_stock": rows[1]["SCODE"] = "601919"
    else: rows.append(rows[-1])
    assert service.update("600887", "financing", refresh=True)["warnings"]
    assert sync_state(db, key) == original
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM financing_daily").fetchone()[0] == 2


def test_legacy_import_is_verified_idempotent_and_does_not_rewrite_original(db, tmp_path):
    root = tmp_path / "chips"
    path = root / "financing" / "600887.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(code="600887", updated_on="2026-09-29", records=[margin("2020-01-02"), margin("2026-09-29")])), encoding="utf-8")
    original = path.read_bytes()
    service = ChipService(db, root)
    assert service.import_legacy("600887", "financing") is True
    assert service.import_legacy("600887", "financing") is False
    assert path.read_bytes() == original
    assert next(path.parent.glob("*.migration-*.bak")).read_bytes() == original
    assert list((db.path.parent / "backups").glob("pre-import-*.sqlite3"))
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM financing_daily").fetchone()[0] == 2
        assert conn.execute("SELECT record_count FROM legacy_imports").fetchone()[0] == 2


def test_legacy_import_query_is_dataset_qualified(db, tmp_path):
    root = tmp_path / "chips"
    path = root / "financing" / "600887.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(code="600887", updated_on="2026-09-29",
                                    records=[margin("2026-09-29")])), encoding="utf-8")
    key = SyncKey(db.ensure_instrument("600887"), "shareholders", "legacy:shareholders")
    run = db.start_sync(key, parser_version="test", methodology_version="test")
    db.complete_sync(run, SyncResult(0, no_data=True),
                     lambda conn: db.record_legacy_import(conn, key, run, str(path.resolve()), "older", 0))
    assert ChipService(db, root).import_legacy("600887", "financing") is True
    with db.connection() as conn:
        assert {r[0] for r in conn.execute("SELECT dataset FROM legacy_imports")} == {"shareholders", "financing"}


def test_bad_legacy_file_does_not_mark_import_or_prevent_valid_source_update(db, tmp_path):
    path = tmp_path / "financing" / "600887.json"
    path.parent.mkdir()
    path.write_text('{"broken":', encoding="utf-8")
    service = ChipService(db, tmp_path, fetcher=lambda *a, **kw: [margin("2026-09-29")])
    result = service.update("600887", "financing")
    assert result["stored_count"] == 1 and result["warnings"]
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM legacy_imports").fetchone()[0] == 0


def test_shareholders_preserve_separate_scope_and_update_without_price_sources(db, tmp_path):
    def fetch(code, section, source, **bounds):
        return [holder("2026-06-30")] if "F10" in source else [holder("2026-05-16", 90, total=False)]
    service = ChipService(db, tmp_path, fetcher=fetch)
    result = service.update("600887", "shareholders")
    assert result["stored_count"] == 2
    assert result["rows"][0]["date"] == "2026-06-30"
    assert [s["scope"] for s in result["series"]] == ["total", "unknown"]
    assert chip_prices(result, "shareholders", [], [], None)["rows"][0]["qfq_close"] is None
    assert db.check()["integrity"] == "ok"


def test_qfq_initial_no_change_append_and_older_revision_rebuild(db):
    source = Prices(history())
    service = PriceService(db, fetcher=source)
    first = service.ensure("600887")
    assert source.calls[0][1] == "1990-01-01"
    expire(db)
    same = service.ensure("600887", refresh=True)
    assert same["version"] == first["version"] and not same["changed"]
    assert source.calls[1][1] == source.rows[-20]["date"]
    source.rows.append(price("2026-09-29", 100))
    expire(db)
    newer = service.ensure("600887", refresh=True)
    assert newer["version"] != first["version"]
    assert len(service.read("600887")) == 61
    assert len(service.read("600887", version=first["version"])) == 60
    source.rows = [price(r["date"], r["open"] - 100) for r in source.rows]
    expire(db)
    rebuilt = service.ensure("600887", refresh=True)
    assert rebuilt["version"] != newer["version"]
    assert source.calls[-1][1] == "1990-01-01"
    assert service.read("600887")[0]["close"] < 0
    assert db.check()["integrity"] == "ok"


def test_old_anchor_change_and_periodic_audit_trigger_full_recheck(db):
    source = Prices(history())
    service = PriceService(db, fetcher=source)
    first = service.ensure("600887")
    source.rows[0] = price(source.rows[0]["date"], -20)
    expire(db)
    second = service.ensure("600887", refresh=True)
    assert second["version"] != first["version"]
    assert source.calls[-1][1] == "1990-01-01"
    expire(db)
    with db.connection(write=True) as conn:
        conn.execute("UPDATE sync_state SET next_full_audit_at='2000-01-01T00:00:00+00:00'")
    audited = service.ensure("600887")
    assert source.calls[-1][1] == "1990-01-01"
    assert audited["version"] == second["version"]


def test_price_failure_and_partial_history_keep_last_complete_generation(db):
    source = Prices(history())
    service = PriceService(db, fetcher=source)
    first = service.ensure("600887")
    source.rows.pop(-5)
    expire(db)
    result = service.ensure("600887", refresh=True)
    assert result["warnings"] and result["version"] == first["version"]
    assert len(service.read("600887")) == 60
    assert db.check()["integrity"] == "ok"


def test_shared_inflight_requests_fetch_once_and_raw_is_independent(db):
    source = Prices(history())
    service = PriceService(db, fetcher=source)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.ensure("600887"), range(4)))
    assert len(source.calls) == 1
    assert len({r["version"] for r in results}) == 1
    service.ensure("600887", "raw")
    assert len(source.calls) == 2
    assert service.read("600887", "raw")[0]["close"] > 0


def test_price_leases_keep_in_use_versions_then_allow_explicit_expiration(db):
    source = Prices(history())
    service = PriceService(db, fetcher=source, anchor_count=0)
    first = service.ensure("600887")["version"]
    service.read("600887", version=first)
    for index in range(2):
        source.rows.append(price(f"2026-09-{29 + index}", 100))
        expire(db)
        service.ensure("600887", refresh=True)
    assert len(service.read("600887", version=first)) == 60
    with db.connection(write=True) as conn:
        conn.execute("UPDATE price_version_leases SET expires_at='2000-01-01T00:00:00+00:00'")
    service.prune("600887")
    with pytest.raises(PriceVersionUnavailable):
        service.read("600887", version=first)


def test_projected_prices_match_only_prior_trades_and_keep_nonpositive_qfq():
    rows = dict(rows=[dict(date="2026-06-28", holders=100), dict(date="2026-07-31", holders=200)])
    result = chip_prices(rows, "shareholders", [price("2026-06-26", 20)], [price("2026-06-26", -2)], 3)
    assert result["rows"][0]["qfq_close"] < 0
    assert result["rows"][0]["raw_close"] > 0
    assert result["rows"][1]["qfq_close"] is None
    assert result["rows"][0]["price_date"] == "2026-06-26"


def test_daily_and_monthly_disclosure_consumers_share_values_without_future_fill():
    qfq = [price("2026-06-26", -2), price("2026-06-29", -1)]
    data = {"reports": [{"publish_date": "2026-06-28"}], "price_basis": "disclosure", "prices": {}}
    assert financial_prices(data, [], qfq)["prices"]["qfq"][0]["date"] == "2026-06-26"
    assert valuation_prices({"rows": [{"date": "2026-06-30", "pe": 10}]}, qfq)["rows"][0]["qfq_close"] == -.99


def test_daily_backup_preserves_state_before_first_update(db, tmp_path, monkeypatch):
    service = ChipService(db, tmp_path, fetcher=lambda *a, **kw: [margin("2026-09-29")])
    calls = []
    original = db.backup
    def backup(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(db, "backup", backup)
    service.update("600887", "financing")
    path = next((tmp_path / "backups").glob("shared-daily-*.sqlite3"))
    snapshot = path.read_bytes()
    assert Database(path).check()["instruments"] == 0
    service.update("300750", "financing", refresh=True)
    assert path.read_bytes() == snapshot
    assert len(calls) == 1
    assert db.check()["instruments"] == 2


def test_daily_backup_rolls_over_without_restart(db, tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from quarterly_dashboard import storage
    class Clock(datetime):
        day = 1
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, cls.day, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(storage, "datetime", Clock)
    service = ChipService(db, tmp_path, fetcher=lambda *a, **kw: [margin("2026-09-29")])
    service.update("600887", "financing")
    Clock.day = 2
    service.update("300750", "financing")
    previous = Database(tmp_path / "backups/shared-daily-2026-10-01.sqlite3")
    current = Database(tmp_path / "backups/shared-daily-2026-10-02.sqlite3")
    assert previous.check()["instruments"] == 0
    assert current.check()["instruments"] == 1
    assert db.check()["instruments"] == 2


def test_concurrent_daily_backup_is_created_once(db, monkeypatch):
    from quarterly_dashboard.update_service import backup_before_update
    calls = []
    original = db.backup
    def backup(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(db, "backup", backup)
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda _: backup_before_update(db), range(10)))
    assert len(calls) == 1


def test_daily_backup_failure_prevents_update(db, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("backup unavailable")
    monkeypatch.setattr(db, "backup", fail)
    service = ChipService(db, tmp_path, fetcher=lambda *a, **kw: pytest.fail("source requested"))
    with pytest.raises(OSError, match="backup unavailable"):
        service.update("600887", "financing")
    assert db.check()["instruments"] == 0


class Response:
    def __init__(self, payload): self.payload = payload
    def json(self): return self.payload
    def raise_for_status(self): pass


def test_price_pagination_overlaps_and_rejects_inconsistent_adjustment():
    data = history(1000)
    class Session:
        def __init__(self, change=False): self.calls, self.change = 0, change
        def get(self, url, params, **kwargs):
            self.calls += 1
            end = params["param"].split(",")[3]
            rows = [r["raw"] for r in data if r["date"] <= end][-640:]
            if self.change and self.calls == 2:
                rows = copy.deepcopy(rows)
                rows[-1][2] = "99999"
            return Response({"code": 0, "data": {"sh600887": {"qfqday": rows}}})
    session = Session()
    result = fetch_price_history("600887", session, "qfq", end=data[-1]["date"])
    assert len(result) == 1000 and session.calls == 2
    with pytest.raises(ValueError, match="复权基准"):
        fetch_price_history("600887", Session(True), "qfq", end=data[-1]["date"])


def test_price_parser_does_not_accept_raw_data_for_requested_qfq():
    with pytest.raises(ValueError):
        parse_price_history({"code": 0, "data": {"sh600887": {"day": [price("2026-09-29")["raw"]]}}}, "sh600887", "qfq")


def test_production_financing_adapter_passes_and_verifies_date_window():
    class Session:
        def get(self, url, params, **kwargs):
            assert params["filter"] == '(SCODE="600887")(DATE>=\'2026-09-01\')(DATE<=\'2026-09-30\')'
            return Response({"success": True, "result": {"count": 1, "pages": 1, "data": [margin("2026-09-29")]}})
    assert len(_fetch_chip_report("600887", "financing", Session(), start_date="2026-09-01", end_date="2026-09-30")) == 1


def test_import_check_time_and_legacy_holder_state_do_not_force_live_refresh(db, tmp_path):
    path = tmp_path / "shareholders" / "600887.json"
    path.parent.mkdir()
    path.write_text(json.dumps(dict(code="600887", updated_on="2020-01-01", records=[holder("2026-06-30")])), encoding="utf-8")
    service = ChipService(db, tmp_path, fetcher=lambda *a, **kw: [holder("2026-06-30")])
    assert service.import_legacy("600887", "shareholders")
    assert service.cached("600887", "shareholders")["needs_update"]
    with db.connection() as conn:
        assert conn.execute("SELECT checked_at FROM sync_state").fetchone()[0].startswith("2020-01-01")
    result = service.update("600887", "shareholders")
    assert not result["needs_update"]
    assert result["series"][0]["source"].endswith("F10_EH_HOLDERNUM")


def test_insufficient_qfq_overlap_rechecks_full_history_before_accepting(db):
    source = Prices(history())
    service = PriceService(db, fetcher=source)
    first = service.ensure("600887")
    calls = []
    def partial_window(code, adjustment, start, end):
        calls.append(start)
        rows = source(code, adjustment, start, end)
        return rows if start == "1990-01-01" else rows[-7:]
    service.fetcher = partial_window
    expire(db)
    result = service.ensure("600887", refresh=True)
    assert calls[-1] == "1990-01-01"
    assert result["version"] == first["version"] and not result["warnings"]
    assert len(service.read("600887")) == 60


def test_committed_version_survives_cleanup_failure(db, monkeypatch):
    source = Prices(history())
    service = PriceService(db, fetcher=source)
    old = service.ensure("600887")["version"]
    source.rows.append(price("2026-09-29", 100))
    expire(db)
    def failed_prune(code):
        raise OSError("cleanup unavailable")
    monkeypatch.setattr(service, "prune", failed_prune)
    result = service.ensure("600887")
    assert result["version"] != old and result["warnings"]
    assert result["version"] == service.current_version("600887")
    assert len(service.read("600887")) == 61
    assert db.check()["integrity"] == "ok"


def test_overlapping_full_audits_share_one_fetch_but_later_audit_runs(db, monkeypatch):
    from quarterly_dashboard import price_service
    started, queued, guard = Event(), Event(), Lock()
    original_lock = price_service.data_lock
    entrants = 0
    def observed_lock(*args):
        nonlocal entrants
        with guard:
            entrants += 1
            if entrants == 3:
                queued.set()
        return original_lock(*args)
    monkeypatch.setattr(price_service, "data_lock", observed_lock)
    source = Prices(history())
    def fetch(*args):
        started.set()
        assert queued.wait(3)
        return source(*args)
    service = PriceService(db, fetcher=fetch)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(service.ensure, "600887", full=True)
        assert started.wait(3)
        followers = [pool.submit(service.ensure, "600887", full=True) for _ in range(2)]
        results = [first.result()] + [f.result() for f in followers]
    assert len(source.calls) == 1
    assert len({r["version"] for r in results}) == 1
    service.ensure("600887", full=True)
    assert len(source.calls) == 2


def test_concurrent_failed_price_refresh_reuses_failure_and_valid_version(db, monkeypatch):
    from quarterly_dashboard import price_service

    source = Prices(history())
    service = PriceService(db, fetcher=source)
    original = service.ensure("600887")["version"]
    expire(db)

    started, queued, release = Event(), Event(), Event()
    original_lock = price_service.data_lock
    entrants = 0
    guard = Lock()

    def observed_lock(*args):
        nonlocal entrants
        with guard:
            entrants += 1
            if entrants == 2:
                queued.set()
        return original_lock(*args)

    def failed_fetch(*args):
        started.set()
        assert release.wait(3)
        raise ValueError("source unavailable")

    monkeypatch.setattr(price_service, "data_lock", observed_lock)
    service.fetcher = failed_fetch
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.ensure, "600887", refresh=True)
        assert started.wait(3)
        follower = pool.submit(service.ensure, "600887", refresh=True)
        assert queued.wait(3)
        release.set()
        results = [first.result(), follower.result()]

    assert all(r["version"] == original and "source unavailable" in r["warnings"][0]
               for r in results)
    assert entrants == 2
    assert service.current_version("600887") == original
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM sync_runs WHERE dataset='prices_adjusted' "
                            "AND status='failed'").fetchone()[0] == 1


@pytest.mark.parametrize("section", ["financing", "shareholders"])
def test_chip_checks_once_per_shanghai_day(db, tmp_path, monkeypatch, section):
    from datetime import datetime, timezone
    from quarterly_dashboard import update_service
    local = timezone(timedelta(hours=8))
    now = datetime.now(local)
    class Clock(datetime):
        offset = timedelta()
        @classmethod
        def now(cls, tz=None):
            return (now + cls.offset).astimezone(tz)
    monkeypatch.setattr(update_service, "datetime", Clock)
    calls = []
    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return [margin("2026-09-29")] if section == "financing" else [holder("2026-06-30")]
    service = ChipService(db, tmp_path, fetcher=fetch)
    service.update("600887", section)
    count = len(calls)
    Clock.offset = timedelta(minutes=11)
    # Stay in the same calendar day regardless of the test execution time.
    if Clock.now(local).date() != now.date():
        Clock.offset = timedelta(minutes=-11)
    service.update("600887", section)
    assert len(calls) == count
    assert not service.cached("600887", section)["needs_update"]
    Clock.offset = timedelta(days=1)
    assert service.cached("600887", section)["needs_update"]
    service.update("600887", section)
    assert len(calls) == count * 2


@pytest.mark.parametrize("adjustment", ["raw", "qfq"])
def test_price_checks_once_per_day_but_manual_refresh_bypasses(db, monkeypatch, adjustment):
    from datetime import datetime, timezone
    from quarterly_dashboard import update_service
    now = datetime.now(timezone.utc)
    class Clock(datetime):
        offset = timedelta()
        @classmethod
        def now(cls, tz=None):
            return (now + cls.offset).astimezone(tz)
    monkeypatch.setattr(update_service, "datetime", Clock)
    source = Prices(history())
    service = PriceService(db, fetcher=source)
    service.ensure("600887", adjustment)
    count = len(source.calls)
    Clock.offset = timedelta(minutes=11)
    if Clock.now(timezone(timedelta(hours=8))).date() != now.astimezone(timezone(timedelta(hours=8))).date():
        Clock.offset = timedelta(minutes=-11)
    service.ensure("600887", adjustment)
    assert len(source.calls) == count
    Clock.offset = timedelta(hours=1)
    service.ensure("600887", adjustment, refresh=True)
    assert len(source.calls) > count
    count = len(source.calls)
    Clock.offset = timedelta(days=1)
    service.ensure("600887", adjustment)
    assert len(source.calls) > count


def test_daily_check_boundary_is_shanghai_midnight(monkeypatch):
    from datetime import datetime, timezone
    from quarterly_dashboard import update_service
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 2, 0, 1, tzinfo=timezone(timedelta(hours=8))).astimezone(tz)
    monkeypatch.setattr(update_service, "datetime", Clock)
    assert not update_service.checked_today({"checked_at": "2026-10-01T15:59:00+00:00"})
    assert update_service.checked_today({"checked_at": "2026-10-01T16:00:00+00:00"})


@pytest.mark.parametrize("dataset, days", [("financial:lrb", 180), ("valuation:pe", 90),
    ("prices_adjusted", 30), ("financing", None), ("prices_raw", None),
    ("shareholders", None), ("dividends", None), ("industry", None)])
def test_audit_policy_by_dataset(dataset, days):
    from datetime import datetime, timezone
    from quarterly_dashboard.update_service import audit_due, next_audit
    due = audit_due({"next_full_audit_at": "2000-01-01T00:00:00+00:00"}, dataset)
    assert due == (days is not None)
    deadline = next_audit(dataset)
    if days is None:
        assert deadline is None
    else:
        assert abs((datetime.fromisoformat(deadline) - datetime.now(timezone.utc)).total_seconds() - days * 86400) < 2


def test_raw_prices_do_not_rebuild_for_old_audit_deadline(db):
    source = Prices(history())
    service = PriceService(db, fetcher=source)
    service.ensure("600887", "raw")
    expire(db)
    with db.connection(write=True) as conn:
        conn.execute("UPDATE sync_state SET next_full_audit_at='2000-01-01T00:00:00+00:00'")
    service.ensure("600887", "raw")
    assert source.calls[-1][1] != "1990-01-01"


def test_financing_overlap_gap_retries_full_once(db, tmp_path):
    rows = [margin("2026-09-28"), margin("2026-09-29")]
    calls = []
    def fetch(*args, **bounds):
        calls.append(bounds)
        return rows if bounds["start_date"] is None else rows[-1:]
    service = ChipService(db, tmp_path, fetcher=fetch)
    service.update("600887", "financing")
    result = service.update("600887", "financing", refresh=True)
    assert not result["warnings"]
    assert len(calls) == 3 and calls[-1]["start_date"] is None
    with db.connection() as conn:
        assert conn.execute("SELECT trigger_reason FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "historical_gap"


def test_raw_price_overlap_gap_retries_full_once(db):
    rows = history()
    calls = []
    def fetch(code, adjustment, start, end):
        calls.append(start)
        return rows if start == "1990-01-01" else rows[-1:]
    service = PriceService(db, fetcher=fetch)
    service.ensure("600887", "raw")
    expire(db)
    result = service.ensure("600887", "raw")
    assert not result["warnings"]
    assert len(calls) == 3 and calls[-1] == "1990-01-01"
    assert len(service.read("600887", "raw")) == len(rows)


def test_p4_page_ignores_old_dividend_and_industry_audit_dates(db, monkeypatch):
    from types import SimpleNamespace
    from quarterly_dashboard.storage import utc_now
    monkeypatch.setattr(server, "services", lambda: (SimpleNamespace(db=db), None))
    monkeypatch.setattr(server, "instrument_id", lambda *args: 1)
    def state(db, key):
        return {"checked_at": utc_now(), "next_full_audit_at":
            "2000-01-01T00:00:00+00:00" if key.dataset in {"industry", "dividends"}
            else "2099-01-01T00:00:00+00:00"}
    monkeypatch.setattr(server, "sync_state", state)
    assert server.p4_loading("600887") == {"financial": False, "dividends": False, "valuation": False}


@pytest.mark.parametrize("span, expected_counts", [(1, [1]), (20, [26]), (650, [640, 40])])
def test_tencent_requests_only_needed_rows_and_keeps_page_overlap(span, expected_counts):
    rows = history(1000)
    counts = []
    class Session:
        def get(self, url, params, **kwargs):
            parts = params["param"].split(",")
            count, end = int(parts[4]), parts[3]
            counts.append(count)
            batch = [row["raw"] for row in rows if row["date"] <= end][-count:]
            return Response({"code": 0, "data": {"sh600887": {"qfqday": batch}}})
    result = fetch_price_history("600887", Session(), "qfq", start=rows[-span]["date"], end=rows[-1]["date"])
    assert len(result) == span
    assert counts == expected_counts


@pytest.mark.parametrize("report, field", [("RPT_F10_EH_HOLDERNUM", "NOTICE_DATE"),
                                           ("RPT_HOLDERNUM_DET", "HOLD_NOTICE_DATE")])
def test_shareholder_adapter_filters_announcement_not_statistical_date(report, field):
    raw = holder("2020-06-30")
    raw.pop("NOTICE_DATE")
    raw[field] = "2026-09-30"
    class Session:
        def get(self, url, params, **kwargs):
            assert params["sortColumns"] == field
            assert f"({field}>='2026-09-01')" in params["filter"]
            assert "END_DATE>=" not in params["filter"]
            return Response({"success": True, "result": {"count": 1, "pages": 1, "data": [raw]}})
    result = _fetch_chip_report("600887", "shareholders", Session(), report,
        start_date="2026-09-01", end_date="2026-10-01", date_field=field)
    assert result == [raw]
    raw[field] = "2026-08-31"
    with pytest.raises(ValueError, match="日期范围"):
        _fetch_chip_report("600887", "shareholders", Session(), report,
            start_date="2026-09-01", end_date="2026-10-01", date_field=field)


def test_shareholder_increment_keeps_history_accepts_late_old_stat_and_empty_check(db, tmp_path):
    stage = [0]
    calls = []
    def fetch(code, section, source, **bounds):
        calls.append((source, bounds))
        if stage[0] == 0:
            return [holder("2020-06-30", 100), holder("2026-06-30", 200)]
        if stage[0] == 1:
            row = holder("2020-06-30", 150)
            row["NOTICE_DATE"] = "2026-09-30"
            return [row]
        return []
    service = ChipService(db, tmp_path, fetcher=fetch)
    service.update("600887", "shareholders")
    stage[0] = 1
    data = service.update("600887", "shareholders", refresh=True)
    assert not data["warnings"] and data["stored_count"] == 4
    for source, bounds in calls[-2:]:
        assert bounds["start_date"] is not None
        assert bounds["date_field"] == ("NOTICE_DATE" if "F10" in source else "HOLD_NOTICE_DATE")
    with db.connection() as conn:
        assert {r[0] for r in conn.execute("SELECT holders FROM shareholder_observations WHERE stat_date='2020-06-30'")} == {150}
    stage[0] = 2
    data = service.update("600887", "shareholders", refresh=True)
    assert not data["warnings"] and data["stored_count"] == 4
    assert not data["needs_update"] and not data["empty"]
    assert db.check()["integrity"] == "ok"


def test_empty_shareholder_source_checks_incrementally_after_initial_check(db, tmp_path):
    calls = []
    def fetch(*args, **bounds):
        calls.append(bounds)
        return []
    service = ChipService(db, tmp_path, fetcher=fetch)
    service.update("600887", "shareholders")
    service.update("600887", "shareholders", refresh=True)
    assert all(b["start_date"] is None for b in calls[:2])
    assert all(b["start_date"] is not None for b in calls[2:])
