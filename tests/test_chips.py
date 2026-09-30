import json
from datetime import date

import pytest

from quarterly_dashboard import server
from quarterly_dashboard.chips import chip_payload, chip_rows, fetch_chip_records, shareholder_price_snapshots


def holder(day="2026-06-30", count=10000):
    return {"SECURITY_CODE": "600887", "END_DATE": day, "HOLDER_TOTAL_NUM": count,
            "NOTICE_DATE": "2026-08-27", "PRICE": 23.96}


def margin(day="2026-09-29", balance=2e9):
    return {"SCODE": "600887", "DATE": day, "RZYE": balance,
            "RZRQYE": 2.4e9, "RZJME": -3e7, "SPJ": 27.24,
            "RQMCL": 67900}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class Session:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params, **kwargs):
        self.calls.append(params)
        return Response(self.pages[params["pageNumber"] - 1])


def page(records, count, pages):
    return {"success": True, "result": {"data": records, "count": count, "pages": pages}}


def test_fetch_reads_all_pages_and_keeps_daily_source_fields():
    session = Session([page([margin()], 2, 2), page([margin("2026-09-28")], 2, 2)])
    records = fetch_chip_records("600887", "financing", session)
    assert [row["DATE"] for row in records] == ["2026-09-28", "2026-09-29"]
    assert records[0]["RQMCL"] == 67900
    assert session.calls[0]["filter"] == '(SCODE="600887")'
    assert len(session.calls) == 2


@pytest.mark.parametrize("pages", [
    [page([margin()], 3, 2), page([margin("2026-09-28")], 3, 2)],
    [page([margin()], 2, 2), page([], 2, 2)],
    [page([margin()], 2, 2), page([margin("2026-09-28")], 3, 2)],
    [page([margin(), margin()], 2, 1)],
    [{"success": False, "message": "error"}],
    [page([{**margin(), "SCODE": "601919"}], 1, 1)],
])
def test_incomplete_changed_duplicate_or_wrong_stock_history_fails(pages):
    with pytest.raises(ValueError):
        fetch_chip_records("600887", "financing", Session(pages))


def test_explicit_no_data_is_distinct_from_source_error():
    session = Session([{"success": False, "code": 9201, "result": None}])
    assert fetch_chip_records("600887", "financing", session) == []


def test_shareholder_dates_are_observations_and_announcement_dates_stay_separate():
    records = [holder("2026-06-30"), holder("2026-05-18", 12000)]
    rows = chip_rows(records, "shareholders")
    assert [row["date"] for row in rows] == ["2026-05-18", "2026-06-30"]
    assert rows[0]["holders"] == 12000
    assert rows[0]["announced_on"] == "2026-08-27"
    assert len(rows) == 2


def test_financing_window_does_not_trim_storage_or_fill_missing_trading_days():
    data = {"records": [margin("2024-01-01"), margin("2025-09-29"), margin("2025-09-30"),
                        margin("2026-09-29"), margin("2026-10-01")]}
    result = chip_payload(data, "financing", today=date(2026, 9, 30))
    assert [row["date"] for row in result["rows"]] == ["2025-09-30", "2026-09-29"]
    assert result["stored_count"] == 5
    assert len(data["records"]) == 5
    assert result["rows"][-1]["net_buy"] == -3e7
    assert result["latest"]["date"] == "2026-09-29"


def test_zero_balances_and_negative_net_buy_are_valid_but_missing_prices_stay_missing():
    record = {**margin(balance=0), "SPJ": None, "RZRQYE": 0}
    row = chip_rows([record], "financing")[0]
    assert row["margin_balance"] == row["total_balance"] == 0
    assert row["net_buy"] < 0
    assert row["close"] is None


@pytest.fixture
def chip_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CHIP_CACHE", tmp_path)
    monkeypatch.setattr(server, "CACHE", tmp_path / "no-financial-data")
    monkeypatch.setattr(server, "fetch_daily_prices", lambda *args: [
        {"date": "2026-06-30", "close": 24}, {"date": "2026-09-15", "close": 25}])
    chips, _ = server.services()
    chips.fetcher = lambda code, section, source, **bounds: (
        [] if source == "RPT_HOLDERNUM_DET" else server.fetch_chip_records(code, section, None))
    return tmp_path


def test_chip_endpoint_is_independent_and_preserves_full_raw_daily_records(chip_cache, monkeypatch):
    monkeypatch.setattr(server, "fetch_chip_records", lambda *args: [margin("2024-01-01"), margin()])
    monkeypatch.setattr(server, "load_stock", lambda *args: pytest.fail("finance should not be needed"))
    result = server.load_chart_data("600887", "financing")
    with server.services()[0].db.connection() as conn:
        stored = conn.execute("SELECT raw_json FROM financing_daily ORDER BY trade_date").fetchall()
    assert len(stored) == 2
    assert json.loads(stored[0][0])["RQMCL"] == 67900
    assert not (chip_cache / "financing" / "600887.json").exists()
    assert result["stored_count"] == 2
    assert len(result["rows"]) == 1
    monkeypatch.setattr(server, "fetch_chip_records", lambda *args: pytest.fail("same-day cache must be reused"))
    assert server.load_chart_data("600887", "financing")["stored_count"] == 2


@pytest.mark.parametrize("outcome", ["missing_date", "missing_required", "empty", "failure"])
def test_partial_refresh_does_not_overwrite_or_backup_good_cache(chip_cache, monkeypatch, outcome):
    monkeypatch.setattr(server, "fetch_chip_records", lambda *args: [margin("2026-09-28"), margin()])
    server.load_chart_data("600887", "financing")
    db = server.services()[0].db
    with db.connection() as conn:
        original = [tuple(r) for r in conn.execute("SELECT * FROM financing_daily")]
    def fetch(*args):
        if outcome == "failure":
            raise ValueError("offline")
        if outcome == "empty":
            return []
        if outcome == "missing_date":
            return [margin()]
        return [margin("2026-09-28"), {**margin(), "RZYE": None}]
    monkeypatch.setattr(server, "fetch_chip_records", fetch)
    result = server.load_chart_data("600887", "financing", True)
    assert result["warnings"]
    assert result["stored_count"] == 2
    with db.connection() as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM financing_daily")] == original


def test_successful_refresh_keeps_backup_and_old_dates(chip_cache, monkeypatch):
    monkeypatch.setattr(server, "fetch_chip_records", lambda *args: [holder()])
    server.load_chart_data("600887", "shareholders")
    db = server.services()[0].db
    original = db.backup(chip_cache / "before-refresh.sqlite3")
    monkeypatch.setattr(server, "fetch_chip_records", lambda *args: [holder(), holder("2026-09-15", 9000)])
    result = server.load_chart_data("600887", "shareholders", True)
    assert result["stored_count"] == 2
    from quarterly_dashboard.storage import Database
    with Database(original).connection() as conn:
        assert conn.execute("SELECT count(*) FROM shareholder_observations").fetchone()[0] == 1
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM shareholder_observations").fetchone()[0] == 2


def test_shareholder_price_never_uses_future_trades_or_long_suspension_prices():
    records = [holder("2026-06-28"), holder("2026-07-31"), holder("2026-06-01")]
    snapshots = shareholder_price_snapshots(records, [
        {"date": "2026-06-26", "close": 22}, {"date": "2026-06-29", "close": 23}])
    assert snapshots == [{"date": "2026-06-28", "price_date": "2026-06-26", "close": 22}]
    rows = chip_rows(records, "shareholders", snapshots)
    assert rows[0]["close"] is None  # Do not treat F10 PRICE as unadjusted close.
    assert rows[1]["price_date"] == "2026-06-26"
    assert rows[2]["close"] is None


def test_shareholders_merge_detail_only_dates_and_prefer_f10_values():
    class Reports(Session):
        def get(self, url, params, **kwargs):
            if params["reportName"] == "RPT_F10_EH_HOLDERNUM":
                return Response(page([holder(count=12000)], 1, 1))
            return Response(page([
                {"SECURITY_CODE": "600887", "END_DATE": "2026-06-30", "HOLDER_NUM": 9000},
                {"SECURITY_CODE": "600887", "END_DATE": "2026-05-18", "HOLDER_NUM": 11000},
            ], 2, 1))
    records = fetch_chip_records("600887", "shareholders", Reports([]))
    rows = chip_rows(records, "shareholders")
    assert [(row["date"], row["holders"]) for row in rows] == [
        ("2026-05-18", 11000), ("2026-06-30", 12000)]


def test_cached_chip_data_embeds_without_network_and_keeps_financial_loading_independent(chip_cache, monkeypatch):
    monkeypatch.setattr(server, "VALUATION_CACHE", chip_cache / "valuation")
    monkeypatch.setattr(server, "fetch_chip_records", lambda *args: [margin()])
    server.load_chart_data("600887", "financing")
    monkeypatch.setattr(server.requests.Session, "request", lambda *args, **kwargs: pytest.fail("page fetched network"))
    page_html = server.render_page("600887", False)
    import re
    data = json.loads(re.search(r'<script id="payload" type="application/json">(.*?)</script>', page_html, re.S).group(1))
    assert data["financing"]["stored_count"] == 1
    assert data["loading"]["financing"] is False
    assert data["loading"]["financial"] is True
    assert data["loading"]["shareholders"] is True
