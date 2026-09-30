"""Dividend facts keep proposals separate and publish only implemented cash."""

import pytest
import json

from quarterly_dashboard.dividend_service import DividendService
from quarterly_dashboard.storage import Database


def _event(proposal, *, status="董事会预案通过", ex_date=None, cash=4.4):
    return {"SECURITY_CODE": "601919", "REPORT_DATE": "2025-12-31 00:00:00",
            "PLAN_NOTICE_DATE": proposal + " 00:00:00", "NOTICE_DATE": "2026-06-18 00:00:00",
            "EX_DIVIDEND_DATE": ex_date + " 00:00:00" if ex_date else None,
            "ASSIGN_PROGRESS": status, "PRETAX_BONUS_RMB": cash,
            "TOTAL_SHARES": 1000000000}


def test_proposal_becomes_implemented_without_duplicate_cash(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    source = [_event("2026-03-20")]
    calls = []

    def fetch(code, *, date_field=None, since=None, report_period=None, page=1):
        calls.append((date_field, since, report_period, page))
        return {"records": list(source), "total": len(source), "pages": 1}

    service = DividendService(db, tmp_path / "legacy", fetch_page=fetch)
    assert service.update("601919")["events"] == []
    assert list((tmp_path / "backups").glob("facts-daily-*.sqlite3"))
    source[:] = [_event("2026-03-20", status="实施分配", ex_date="2026-06-26")]
    result = service.update("601919", refresh=True)
    assert len(result["events"]) == 1
    assert result["events"][0]["per_share"] == pytest.approx(.44)
    assert any(field == "NOTICE_DATE" for field, *_ in calls)
    assert any(field == "EX_DIVIDEND_DATE" for field, *_ in calls)
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM dividend_events").fetchone()[0] == 1


def test_same_report_period_with_two_proposal_dates_counts_both(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    source = [_event("2026-03-20", status="实施分配", ex_date="2026-06-26"),
              _event("2026-05-20", status="实施分配", ex_date="2026-07-26", cash=2)]

    def fetch(code, *, date_field=None, since=None, report_period=None, page=1):
        return {"records": source, "total": 2, "pages": 1}

    service = DividendService(db, tmp_path / "legacy", fetch_page=fetch)
    events = service.update("601919")["events"]
    assert len(events) == 2
    assert sorted(event["per_share"] for event in events) == pytest.approx([.2, .44])


def test_source_failure_keeps_existing_implemented_event(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    broken = False

    def fetch(code, *, date_field=None, since=None, report_period=None, page=1):
        if broken:
            raise ValueError("offline")
        return {"records": [_event("2026-03-20", status="实施分配", ex_date="2026-06-26")],
                "total": 1, "pages": 1}

    service = DividendService(db, tmp_path / "legacy", fetch_page=fetch)
    service.update("601919")
    broken = True
    result = service.update("601919", refresh=True)
    assert len(result["events"]) == 1
    assert result["warnings"]


def test_confirmed_no_dividend_uses_date_windows_on_refresh(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    calls = []

    def fetch(code, *, date_field=None, since=None, report_period=None, page=1):
        calls.append((date_field, since))
        return {"records": [], "total": 0, "pages": 0}

    service = DividendService(db, tmp_path / "legacy", fetch_page=fetch)
    assert service.update("601919")["events"] == []
    assert calls == [(None, None)]
    calls.clear()
    assert service.update("601919", refresh=True)["events"] == []
    assert {field for field, _ in calls} == {"NOTICE_DATE", "EX_DIVIDEND_DATE"}


def test_legacy_dividend_import_shares_file_with_financial_import(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "601919.json").write_text(json.dumps({"code": "601919", "reports": [],
        "dividend_events": [{"date": "2026-06-26", "report_period": "2025-12-31",
                             "per_share": .44, "total_shares": 1000000000}]}), encoding="utf-8")
    service = DividendService(db, legacy)
    service.import_legacy("601919")
    service.import_legacy("601919")
    assert service.read("601919")[0]["per_share"] == pytest.approx(.44)
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM legacy_imports WHERE dataset='dividends'").fetchone()[0] == 1


def test_revised_proposal_date_for_same_paid_event_keeps_internal_identity(tmp_path):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    source = [_event("2026-03-20", status="实施分配", ex_date="2026-06-26")]

    def fetch(code, *, date_field=None, since=None, report_period=None, page=1):
        return {"records": list(source), "total": len(source), "pages": 1}

    service = DividendService(db, tmp_path / "legacy", fetch_page=fetch)
    service.update("601919")
    with db.connection() as conn:
        original_id = conn.execute("SELECT id FROM dividend_events").fetchone()[0]
    source[:] = [_event("2026-03-21", status="实施分配", ex_date="2026-06-26")]
    result = service.update("601919", refresh=True)
    assert len(result["events"]) == 1
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM dividend_events").fetchone()[0] == 1
        assert conn.execute("SELECT id FROM dividend_events").fetchone()[0] == original_id
