"""Offline contracts from the 2026-09-30 P4 source probes.

Fixtures are reduced copies of recorded Sina, EastMoney, and Baidu replies.
Only the remote HTTP boundary is substituted; adapter parsing and validation run.
"""

import copy
import json
from pathlib import Path

import pytest

from quarterly_dashboard import sources, valuation


FIXTURES = Path(__file__).parent / "fixtures" / "p4"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class Session:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.calls = []

    def get(self, url, params, **kwargs):
        self.calls.append((url, params))
        return Response(next(self.payloads))


def sina_page(periods, total=3):
    sample = fixture("sina_pages.json")
    data = sample["result"]["data"]
    data["report_count"] = str(total)
    data["report_list"] = {key: data["report_list"].get(key, {"data": []}) for key in periods}
    data["report_date"] = [{"date_value": key} for key in periods]
    return sample


@pytest.mark.parametrize("source", ["lrb", "gjzb"])
def test_sina_page_distinguishes_full_page_last_page_and_empty_page(source):
    session = Session([
        sina_page(["20260630", "20260331"]),
        sina_page(["20251231"]),
        sina_page([]),
    ])
    first = sources.fetch_financial_report_page("000001", session, source, num=2, page=1)
    last = sources.fetch_financial_report_page("000001", session, source, num=2, page=2)
    empty = sources.fetch_financial_report_page("000001", session, source, num=2, page=3)
    assert first["total"] == last["total"] == empty["total"] == 3
    assert list(first["records"]) == ["20260630", "20260331"]
    assert list(last["records"]) == ["20251231"]
    assert empty["records"] == {}
    assert (first["newest_period"], first["oldest_period"]) == ("2026-06-30", "2026-03-31")
    assert empty["oldest_period"] is None
    assert [params for _, params in session.calls] == [
        {"paperCode": "sz000001", "source": source, "type": "0", "page": str(page), "num": "2"}
        for page in (1, 2, 3)
    ]


@pytest.mark.parametrize("source", ["lrb", "fzb", "llb", "gjzb"])
def test_sina_requests_each_statement_type_separately(source):
    session = Session([sina_page(["20260630"], total=1)])
    result = sources.fetch_financial_report_page("601919", session, source, num=8)
    assert result["total"] == 1
    assert session.calls[0][1]["source"] == source
    assert session.calls[0][1]["num"] == "8"


@pytest.mark.parametrize("payload", [
    sina_page(["20260630"], total=3),
    sina_page(["20260331", "20260630"], total=3),
    sina_page(["20260630", "20260630"], total=3),
])
def test_sina_rejects_missing_or_unordered_page_records(payload):
    with pytest.raises(ValueError):
        sources.fetch_financial_report_page("601919", Session([payload]), "lrb", num=2)


def test_sina_rejects_invalid_code_source_and_page_without_network():
    session = Session([])
    for args in [("invalid", "lrb", 8, 1), ("601919", "bad", 8, 1),
                 ("601919", "lrb", 0, 1), ("601919", "lrb", 8, 0)]:
        with pytest.raises(ValueError):
            sources.fetch_financial_report_page(args[0], session, args[1], num=args[2], page=args[3])
    assert session.calls == []


def test_sina_rejects_source_error_even_if_records_are_present():
    payload = sina_page(["20260630"], total=1)
    payload["result"]["status"]["code"] = 1
    with pytest.raises(ValueError):
        sources.fetch_financial_report_page("601919", Session([payload]), "lrb")


def test_sina_rejects_invalid_report_period():
    with pytest.raises(ValueError):
        sources.fetch_financial_report_page(
            "601919", Session([sina_page(["20260230"], total=1)]), "lrb")


def test_bank_statement_raw_fields_remain_parseable():
    income = sina_page(["20260630"], total=1)
    balance = sina_page(["20260630"], total=1)
    balance["result"]["data"]["report_list"]["20260630"]["data"] = [
        {"item_title": "股本", "item_value": "19406000000.000000"},
        {"item_title": "归属于母公司股东权益合计", "item_value": "548214000000.000000"},
    ]
    row = sources.parse_financial_reports(income, balance)[0]
    assert row["revenue_ytd"] == 70617000000.0
    assert row["shares"] == 19406000000.0
    assert row["equity"] == 548214000000.0


def dividend_page(records, total=2, pages=2):
    sample = fixture("dividend_page.json")
    sample["result"] = {"count": total, "pages": pages, "data": records}
    return sample


def test_dividend_page_keeps_proposal_and_filters_by_notice_date():
    sample = fixture("dividend_page.json")
    session = Session([sample])
    page = valuation.fetch_dividend_event_page(
        "601919", session, date_field="NOTICE_DATE", since="2026-08-01", page=1, page_size=1)
    assert page["total"] == 2 and page["pages"] == 2
    assert page["records"][0]["ASSIGN_PROGRESS"] == "董事会决议通过"
    assert page["records"][0]["EX_DIVIDEND_DATE"] is None
    assert page["records"][0]["PRETAX_BONUS_RMB"] == 4.3
    assert session.calls[0][1]["filter"] == '(SECURITY_CODE="601919")(NOTICE_DATE>=\'2026-08-01\')'
    assert session.calls[0][1]["pageNumber"] == "1"
    assert session.calls[0][1]["pageSize"] == "1"


def test_dividend_page_supports_ex_date_and_last_empty_pages():
    record = fixture("dividend_page.json")["result"]["data"][0]
    record["EX_DIVIDEND_DATE"] = "2026-09-15 00:00:00"
    session = Session([dividend_page([record]), dividend_page([], total=0, pages=0)])
    last = valuation.fetch_dividend_event_page(
        "601919", session, date_field="EX_DIVIDEND_DATE", since="2026-09-01", page=2, page_size=1)
    empty = valuation.fetch_dividend_event_page(
        "601919", session, date_field="NOTICE_DATE", since="2026-09-29")
    assert last["records"] == [record]
    assert empty == {"records": [], "total": 0, "pages": 0}
    assert "EX_DIVIDEND_DATE>='2026-09-01'" in session.calls[0][1]["filter"]


def test_dividend_empty_window_accepts_eastmoney_no_data_code():
    session = Session([{"success": False, "code": 9201, "result": None}])
    assert valuation.fetch_dividend_event_page(
        "601919", session, date_field="NOTICE_DATE", since="2026-09-29") == {
            "records": [], "total": 0, "pages": 0}


def test_dividend_page_rejects_wrong_stock_ignored_filter_and_missing_page():
    record = fixture("dividend_page.json")["result"]["data"][0]
    wrong = copy.deepcopy(record)
    wrong["SECURITY_CODE"] = "600887"
    stale = copy.deepcopy(record)
    stale["NOTICE_DATE"] = "2026-07-01"
    for payload in [dividend_page([wrong], total=1, pages=1),
                    dividend_page([stale], total=1, pages=1),
                    dividend_page([], total=2, pages=2),
                    {"success": False, "code": 9201, "result": None}]:
        with pytest.raises(ValueError):
            valuation.fetch_dividend_event_page(
                "601919", Session([payload]), date_field="NOTICE_DATE",
                since="2026-08-01", page=2 if payload.get("code") == 9201 else 1,
                page_size=1)


def test_dividend_page_validates_filter_arguments_before_network():
    session = Session([])
    for options in [{"date_field": "BAD", "since": "2026-01-01"},
                    {"date_field": "NOTICE_DATE", "since": "bad"},
                    {"date_field": "NOTICE_DATE"}, {"page": 0}]:
        with pytest.raises(ValueError):
            valuation.fetch_dividend_event_page("601919", session, **options)
    assert session.calls == []


def test_dividend_report_period_filter_rejects_source_that_ignores_it():
    record = fixture("dividend_page.json")["result"]["data"][0]
    session = Session([dividend_page([record], total=1, pages=1)])
    page = valuation.fetch_dividend_event_page(
        "601919", session, report_period="2026-06-30")
    assert page["records"] == [record]
    assert session.calls[0][1]["filter"] == (
        '(SECURITY_CODE="601919")(REPORT_DATE="2026-06-30")')
    with pytest.raises(ValueError):
        valuation.fetch_dividend_event_page(
            "601919", Session([dividend_page([record], total=1, pages=1)]),
            report_period="2025-12-31")


@pytest.mark.parametrize("window", ["近十年", "近五年", "近三年", "近一年"])
def test_baidu_indicator_uses_selected_source_window(window):
    payload = fixture("baidu_year.json")
    payload["Result"][0]["DisplayData"]["resultData"]["tplData"]["result"]["chartInfo"][0]["type"] = window
    session = Session([payload])
    points = valuation.fetch_valuation_indicator("601919", session, "pe", window)
    assert points == [{"date": "2025-09-30", "value": 4.46},
                      {"date": "2026-09-30", "value": 9.34}]
    assert session.calls[0][1]["chart_select"] == window
    assert session.calls[0][1]["query"] == "市盈率(TTM)"


def test_baidu_indicator_rejects_invalid_window_and_empty_data():
    session = Session([])
    with pytest.raises(ValueError):
        valuation.fetch_valuation_indicator("601919", session, "pe", "全部")
    with pytest.raises(ValueError):
        valuation.fetch_valuation_indicator("601919", session, "bad", "近一年")
    assert session.calls == []
    payload = fixture("baidu_year.json")
    payload["Result"][0]["DisplayData"]["resultData"]["tplData"]["result"]["chartInfo"][0]["body"] = []
    with pytest.raises(ValueError):
        valuation.fetch_valuation_indicator("601919", Session([payload]), "pe", "近一年")
    payload = fixture("baidu_year.json")
    with pytest.raises(ValueError):
        valuation.fetch_valuation_indicator("601919", Session([payload]), "pe", "近三年")
