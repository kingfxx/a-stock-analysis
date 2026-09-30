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
from quarterly_dashboard.storage import Database, SyncKey, instance_lock
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
    assert calls[1]["start_date"] == "2026-08-30"
    assert data["rows"][0]["margin_balance"] == 90
    with db.connection() as conn:
        assert conn.execute("SELECT margin_balance FROM financing_daily WHERE trade_date='2020-01-02'").fetchone()[0] == 100
    assert db.check()["integrity"] == "ok"


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
        assert json.loads(row["canonical_extra_json"])["RQMCL"] == 67900
        assert json.loads(row["field_provenance_json"])["RQMCL"] != row["run_id"]
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


def test_daily_backup_replaces_current_day_after_successful_update(db, tmp_path):
    path = db.daily_backup()
    assert Database(path).check()["instruments"] == 0
    service = ChipService(db, tmp_path, fetcher=lambda *a, **kw: [margin("2026-09-29")])
    service.update("600887", "financing")
    assert Database(path).check()["instruments"] == 1


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
