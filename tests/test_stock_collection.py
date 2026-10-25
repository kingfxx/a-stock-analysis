import json
from datetime import datetime, timedelta, timezone

import requests

from stock_analysis.collection.collection import ProviderBlocked, RequestCollector, begin_batch
from stock_analysis.infrastructure.network import create_data_session
from stock_analysis.domains.stocks.stock_collection import routed_stock_requests
from stock_analysis.infrastructure.storage import Database, SyncKey, SyncResult
from stock_analysis.domains.financials.fundamental_service import FundamentalService
from stock_analysis.domains.stocks.update_service import recent_sync_failure


class Clock:
    now = 10_000

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self.response


class SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


def json_response(payload):
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(payload, ensure_ascii=False).encode()
    response.headers["Content-Type"] = "application/json"
    return response


def expired_provider_cooldown(db, provider, clock):
    expired = datetime.fromtimestamp(clock.time() - 1, timezone.utc).isoformat()
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO collection_provider_state(source_system,cooldown_until,reason,denials,budget_json,updated_at) "
                     "VALUES(?,?,'denied',1,'{}','test')", (provider, expired))


def test_metadata_only_records_request_without_archiving_response(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:7", "stock.financing", "eastmoney",
                        scope={"evidence_mode": "metadata_only", "sync_run_id": 7})
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"result":{"data":[]}}'
    response.headers["Content-Type"] = "application/json"
    clock = Clock()
    collector = RequestCollector(db, session=Session(response), clock=clock.time,
                                 sleep=clock.sleep, uniform=lambda *_: 0)

    returned, source_id = collector.get(batch, "eastmoney", "stock.financing", "https://example.test/data",
                                        {"code": "600000"}, evidence_mode="metadata_only")

    assert returned is response
    assert not list((tmp_path / "collection_sources").rglob("*.raw.gz"))
    with db.connection() as conn:
        source = conn.execute("SELECT * FROM collection_sources WHERE id=?", (source_id,)).fetchone()
    assert source["request_status"] == "success"
    assert source["parse_status"] == "pending"
    assert source["file_path"] is None
    assert source["raw_sha256"] is None
    assert source["stored_sha256"] is None
    assert json.loads(source["request_json"])["params"] == {"code": "600000"}


def test_metadata_only_rejects_unknown_evidence_mode(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:7", "stock.financing", "eastmoney")
    collector = RequestCollector(db, session=Session(None), clock=lambda: 10_000,
                                 sleep=lambda _: None, uniform=lambda *_: 0)

    try:
        collector.get(batch, "eastmoney", "stock.financing", "https://example.test/data",
                      evidence_mode="discard")
    except ValueError as exc:
        assert "证据策略" in str(exc)
    else:
        raise AssertionError("unsupported evidence strategy was accepted")


def test_stock_requests_skip_public_wait_but_remain_in_shared_budget(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    stock_batch = begin_batch(db, "sync_runs:8", "stock.financing", "eastmoney",
                              scope={"evidence_mode": "metadata_only"}, domain="stock")
    market_batch = begin_batch(db, "market-operation", "market.test", "eastmoney")
    clock = Clock()

    class TimedSession:
        def __init__(self):
            self.calls = []

        def get(self, *args, **kwargs):
            self.calls.append(clock.time())
            clock.sleep(2)
            return json_response({"code": 9201, "result": None})

    session = TimedSession()
    collector = RequestCollector(db, session=session, clock=clock.time, sleep=clock.sleep,
                                 uniform=lambda *_: 0)
    with routed_stock_requests(db, stock_batch, "eastmoney", "financing", collector=collector):
        with create_data_session() as routed:
            for page in range(6):
                routed.get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                           params={"reportName": "RPTA_WEB_RZRQ_GGMX", "pageNumber": page + 1})

    assert len(session.calls) == 6
    assert all(later - earlier == 2 for earlier, later in zip(session.calls, session.calls[1:]))
    budget = json.loads(collector.state("eastmoney")["budget_json"])
    assert len(budget["starts"]) == 6

    collector.get(market_batch, "eastmoney", "market.test", "https://example.test/next",
                  evidence_mode="metadata_only")
    assert session.calls[-1] - session.calls[0] >= 60


def test_stock_session_routes_real_get_and_finalizes_parse_metadata(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:9", "stock.financing", "eastmoney",
                        scope={"evidence_mode": "metadata_only", "sync_run_id": 9})
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"success":true,"result":{"data":[]}}'
    response.headers["Content-Type"] = "application/json"
    clock = Clock()
    collector = RequestCollector(db, session=Session(response), clock=clock.time,
                                 sleep=clock.sleep, uniform=lambda *_: 0)

    with routed_stock_requests(db, batch, "eastmoney", "financing", collector=collector):
        with create_data_session() as session:
            assert session.get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                               params={"reportName": "RPTA_WEB_RZRQ_GGMX"}).json()["success"]

    with db.connection() as conn:
        source = conn.execute("SELECT * FROM collection_sources WHERE batch_id=?", (batch,)).fetchone()
    assert source["parse_status"] == "complete"
    assert source["file_path"] is None
    assert source["raw_sha256"] is None


def test_page_parse_status_does_not_claim_batch_validation_success(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    identity = db.ensure_instrument("600519")
    run = db.start_sync(SyncKey(identity, "financing", "eastmoney"),
                        parser_version="test", methodology_version="test")
    batch = begin_batch(db, f"sync_runs:{run}", "stock.financing", "eastmoney", domain="stock",
                        scope={"evidence_mode": "metadata_only"})
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"success":true,"result":{"data":[]}}'
    response.headers["Content-Type"] = "application/json"
    clock = Clock()
    collector = RequestCollector(db, session=Session(response), clock=clock.time,
                                 sleep=clock.sleep, uniform=lambda *_: 0)

    with routed_stock_requests(db, batch, "eastmoney", "financing", collector=collector):
        with create_data_session() as session:
            session.get("https://datacenter-web.eastmoney.com/api/data/v1/get").json()
    db.fail_sync(run, "cross-page validation failed", collection_batch_id=batch)

    with db.connection() as conn:
        source = conn.execute("SELECT parse_status FROM collection_sources WHERE batch_id=?", (batch,)).fetchone()
        statuses = conn.execute("SELECT r.status,b.status FROM sync_runs r JOIN collection_batches b "
                                "ON b.operation_id='sync_runs:'||r.id WHERE r.id=?", (run,)).fetchone()
    assert source[0] == "complete"  # This is the response page parse status only.
    assert tuple(statuses) == ("failed", "failed")  # Batch-level validation remains separate.


def test_financial_sync_run_and_public_batch_complete_together(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()

    def fetch_page(code, kind, num, page):
        report = {"publish_date": "20261001", "data": []}
        if kind == "lrb":
            report["data"] = [{"item_title": "营业收入", "item_value": "10"},
                              {"item_title": "归属于母公司所有者的净利润", "item_value": "2"}]
        return {"records": {"20260930": report}, "total": 1}

    service = FundamentalService(db, tmp_path / "legacy", fetch_page=fetch_page)
    service.update("600519")

    with db.connection() as conn:
        batches = conn.execute("SELECT b.*,r.status AS sync_status FROM collection_batches b "
                               "JOIN sync_runs r ON b.operation_id='sync_runs:'||r.id "
                               "WHERE b.domain='stock' ORDER BY b.id").fetchall()
    assert len(batches) == 4
    assert all(row["status"] == "complete" and row["sync_status"] == "success" for row in batches)
    assert all(json.loads(row["scope_json"])["evidence_mode"] == "metadata_only" for row in batches)


def test_interrupted_stock_batch_recovers_with_its_sync_run(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    identity = db.ensure_instrument("600519")
    run = db.start_sync(SyncKey(identity, "financing", "eastmoney"),
                        parser_version="test", methodology_version="test")
    batch = begin_batch(db, f"sync_runs:{run}", "stock.financing", "eastmoney", domain="stock")

    assert db.recover_interrupted_runs() == 1

    with db.connection() as conn:
        status = conn.execute("SELECT r.status,b.status FROM sync_runs r JOIN collection_batches b "
                              "ON b.operation_id='sync_runs:'||r.id WHERE r.id=? AND b.id=?",
                              (run, batch)).fetchone()
    assert tuple(status) == ("failed", "failed")


def test_recent_sync_failure_supports_a_ten_minute_automatic_quiet_period(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    identity = db.ensure_instrument("600519")
    key = SyncKey(identity, "financial:lrb", "sina")
    now = datetime.now(timezone.utc)
    run = db.start_sync(key, parser_version="test", methodology_version="test")
    db.fail_sync(run, "offline")

    assert recent_sync_failure(db, key, now=now + timedelta(seconds=599))
    assert recent_sync_failure(db, key, now=now + timedelta(seconds=601)) is None


def test_later_no_data_run_clears_previous_failure_quiet_period(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    key = SyncKey(db.ensure_instrument("600519"), "financial:lrb", "sina")
    failed = db.start_sync(key, parser_version="test", methodology_version="test")
    db.fail_sync(failed, "offline")
    succeeded = db.start_sync(key, parser_version="test", methodology_version="test")
    db.complete_sync(succeeded, SyncResult(0, no_data=True), lambda conn: None)

    assert recent_sync_failure(db, key) is None


def test_later_superseded_run_clears_previous_failure_quiet_period(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    key = SyncKey(db.ensure_instrument("600519"), "financial:lrb", "sina")
    failed = db.start_sync(key, parser_version="test", methodology_version="test")
    db.fail_sync(failed, "offline")
    superseded = db.start_sync(key, parser_version="test", methodology_version="test")
    db.fail_sync(superseded, "newer result committed", superseded=True)

    assert recent_sync_failure(db, key) is None


def test_expired_baidu_cooldown_requires_metric_response_structure(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:9", "stock.valuation", "baidu", domain="stock")
    bad = requests.Response()
    bad.status_code = 200
    bad._content = b'{"Result":[{}]}'
    bad.headers["Content-Type"] = "application/json"
    session = Session(bad)
    clock = Clock()
    collector = RequestCollector(db, session=session, clock=clock.time, sleep=clock.sleep,
                                 uniform=lambda *_: 0)
    expired = datetime.fromtimestamp(clock.time() - 1, timezone.utc).isoformat()
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO collection_provider_state(source_system,cooldown_until,reason,denials,budget_json,updated_at) "
                     "VALUES('baidu',?,'denied',1,'{}','test')", (expired,))

    from stock_analysis.domains.valuation.valuation import fetch_valuation_indicator
    with routed_stock_requests(db, batch, "baidu", "valuation", collector=collector):
        with create_data_session() as routed:
            try:
                fetch_valuation_indicator("600519", routed, "pe")
            except ProviderBlocked:
                pass
            else:
                raise AssertionError("invalid recovery response cleared provider cooldown")
            try:
                fetch_valuation_indicator("600519", routed, "pb")
            except ProviderBlocked:
                pass
            else:
                raise AssertionError("second same-provider request escaped cooldown")

    assert session.calls == 1
    with db.connection() as conn:
        state = conn.execute("SELECT cooldown_until FROM collection_provider_state WHERE source_system='baidu'").fetchone()
    assert state[0] is not None


def test_expired_baidu_cooldown_clears_only_after_valid_chart_response(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:10", "stock.valuation", "baidu", domain="stock")
    payload = {"Result": [{"DisplayData": {"resultData": {"tplData": {"result": {
        "chartInfo": [{"body": [["2026-01-01", "10"]], "header": ["市盈率(TTM)"],
                       "type": "近十年"}]}}}}}]}
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(payload, ensure_ascii=False).encode()
    response.headers["Content-Type"] = "application/json"
    clock = Clock()
    collector = RequestCollector(db, session=Session(response), clock=clock.time, sleep=clock.sleep,
                                 uniform=lambda *_: 0)
    expired = datetime.fromtimestamp(clock.time() - 1, timezone.utc).isoformat()
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO collection_provider_state(source_system,cooldown_until,reason,denials,budget_json,updated_at) "
                     "VALUES('baidu',?,'denied',1,'{}','test')", (expired,))

    from stock_analysis.domains.valuation.valuation import fetch_valuation_indicator
    with routed_stock_requests(db, batch, "baidu", "valuation", collector=collector):
        with create_data_session() as routed:
            assert fetch_valuation_indicator("600519", routed, "pe")[0]["value"] == 10
    with db.connection() as conn:
        state = conn.execute("SELECT cooldown_until FROM collection_provider_state WHERE source_system='baidu'").fetchone()
    assert state[0] is None


def test_expired_sina_cooldown_rejects_report_count_list_mismatch(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:11", "stock.financial", "sina", domain="stock")
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"result":{"status":{"code":0},"data":{"report_count":1,"report_list":{}}}}'
    response.headers["Content-Type"] = "application/json"
    clock = Clock()
    collector = RequestCollector(db, session=Session(response), clock=clock.time, sleep=clock.sleep,
                                 uniform=lambda *_: 0)
    expired = datetime.fromtimestamp(clock.time() - 1, timezone.utc).isoformat()
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO collection_provider_state(source_system,cooldown_until,reason,denials,budget_json,updated_at) "
                     "VALUES('sina',?,'denied',1,'{}','test')", (expired,))

    from stock_analysis.domains.stocks.sources import fetch_financial_report_page
    with routed_stock_requests(db, batch, "sina", "financial", collector=collector):
        with create_data_session() as routed:
            try:
                fetch_financial_report_page("600519", routed, "lrb")
            except ProviderBlocked:
                pass
            else:
                raise AssertionError("Sina recovery accepted a report_count/list mismatch")


def test_expired_sina_cooldown_accepts_valid_report_page(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:19", "stock.financial", "sina", domain="stock")
    payload = {"result": {"status": {"code": 0}, "data": {
        "report_count": 1, "report_list": {"20260930": {"data": []}}}}}
    clock = Clock()
    collector = RequestCollector(db, session=Session(json_response(payload)), clock=clock.time,
                                 sleep=clock.sleep, uniform=lambda *_: 0)
    expired_provider_cooldown(db, "sina", clock)

    from stock_analysis.domains.stocks.sources import fetch_financial_report_page
    with routed_stock_requests(db, batch, "sina", "financial", collector=collector):
        with create_data_session() as routed:
            assert fetch_financial_report_page("600519", routed, "lrb")["total"] == 1
    assert collector.state("sina")["cooldown_until"] is None


def test_expired_eastmoney_cooldown_rejects_invalid_chip_pagination(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:12", "stock.financing", "eastmoney", domain="stock")
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"success":true,"result":{"data":[{}],"count":1,"pages":1}}'
    response.headers["Content-Type"] = "application/json"
    clock = Clock()
    collector = RequestCollector(db, session=Session(response), clock=clock.time, sleep=clock.sleep,
                                 uniform=lambda *_: 0)
    expired = datetime.fromtimestamp(clock.time() - 1, timezone.utc).isoformat()
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO collection_provider_state(source_system,cooldown_until,reason,denials,budget_json,updated_at) "
                     "VALUES('eastmoney',?,'denied',1,'{}','test')", (expired,))

    from stock_analysis.domains.stocks.chips import _fetch_chip_report
    with routed_stock_requests(db, batch, "eastmoney", "financing", collector=collector):
        with create_data_session() as routed:
            try:
                _fetch_chip_report("600519", "financing", routed)
            except ProviderBlocked:
                pass
            else:
                raise AssertionError("EastMoney recovery accepted a missing stock node")


def test_expired_eastmoney_cooldown_accepts_valid_chip_page(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:20", "stock.financing", "eastmoney", domain="stock")
    payload = {"success": True, "result": {"count": 1, "pages": 1,
        "data": [{"SCODE": "600519", "DATE": "2026-01-30", "RZYE": "10", "SPJ": "10"}]}}
    clock = Clock()
    collector = RequestCollector(db, session=Session(json_response(payload)), clock=clock.time,
                                 sleep=clock.sleep, uniform=lambda *_: 0)
    expired_provider_cooldown(db, "eastmoney", clock)

    from stock_analysis.domains.stocks.chips import _fetch_chip_report
    with routed_stock_requests(db, batch, "eastmoney", "financing", collector=collector):
        with create_data_session() as routed:
            assert _fetch_chip_report("600519", "financing", routed)[0]["SCODE"] == "600519"
    assert collector.state("eastmoney")["cooldown_until"] is None


def test_expired_eastmoney_cooldown_accepts_explicit_no_data_protocol(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:21", "stock.financing", "eastmoney", domain="stock")
    clock = Clock()
    collector = RequestCollector(db, session=Session(json_response({"code": 9201, "result": None})),
                                 clock=clock.time, sleep=clock.sleep, uniform=lambda *_: 0)
    expired_provider_cooldown(db, "eastmoney", clock)

    from stock_analysis.domains.stocks.chips import _fetch_chip_report
    with routed_stock_requests(db, batch, "eastmoney", "financing", collector=collector):
        with create_data_session() as routed:
            assert _fetch_chip_report("600519", "financing", routed) == []
    assert collector.state("eastmoney")["cooldown_until"] is None


def test_expired_tencent_cooldown_rejects_missing_requested_stock_node(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:13", "stock.prices", "tencent", domain="stock")
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"code":0,"data":{}}'
    response.headers["Content-Type"] = "application/json"
    clock = Clock()
    collector = RequestCollector(db, session=Session(response), clock=clock.time, sleep=clock.sleep,
                                 uniform=lambda *_: 0)
    expired = datetime.fromtimestamp(clock.time() - 1, timezone.utc).isoformat()
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO collection_provider_state(source_system,cooldown_until,reason,denials,budget_json,updated_at) "
                     "VALUES('tencent',?,'denied',1,'{}','test')", (expired,))

    from stock_analysis.domains.stocks.sources import fetch_price_history
    with routed_stock_requests(db, batch, "tencent", "prices", collector=collector):
        with create_data_session() as routed:
            try:
                fetch_price_history("600519", routed, "", start="2026-01-01", end="2026-01-31")
            except ProviderBlocked:
                pass
            else:
                raise AssertionError("Tencent recovery accepted a missing requested stock node")


def test_expired_tencent_cooldown_accepts_matching_stock_name_quote(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:14", "stock.name", "tencent", domain="stock")
    response = requests.Response()
    response.status_code = 200
    response._content = b'v_sh600519="1~TestCompany~600519";'
    clock = Clock()
    collector = RequestCollector(db, session=Session(response), clock=clock.time, sleep=clock.sleep,
                                 uniform=lambda *_: 0)
    expired_provider_cooldown(db, "tencent", clock)

    from stock_analysis.domains.stocks.sources import fetch_stock_name
    with routed_stock_requests(db, batch, "tencent", "name", collector=collector):
        with create_data_session() as routed:
            assert fetch_stock_name("600519", routed) == "TestCompany"
    assert collector.state("tencent")["cooldown_until"] is None


def test_expired_tencent_cooldown_rejects_wrong_stock_name_quote(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:15", "stock.name", "tencent", domain="stock")
    response = requests.Response()
    response.status_code = 200
    response._content = b'v_sz600519="1~WrongMarket~600519";'
    clock = Clock()
    collector = RequestCollector(db, session=Session(response), clock=clock.time, sleep=clock.sleep,
                                 uniform=lambda *_: 0)
    expired_provider_cooldown(db, "tencent", clock)

    from stock_analysis.collection.collection import ProviderBlocked
    from stock_analysis.domains.stocks.sources import fetch_stock_name
    with routed_stock_requests(db, batch, "tencent", "name", collector=collector):
        with create_data_session() as routed:
            try:
                fetch_stock_name("600519", routed)
            except ProviderBlocked:
                pass
            else:
                raise AssertionError("Tencent accepted a quote for a different market symbol")


def test_expired_tencent_qfq_cooldown_accepts_direct_qfqday(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:16", "stock.prices", "tencent", domain="stock")
    payload = {"code": 0, "data": {"sh600519": {"qfqday": [
        ["2026-01-30", "10", "10", "11", "9", "100"]]}}}
    clock = Clock()
    collector = RequestCollector(db, session=Session(json_response(payload)), clock=clock.time,
                                 sleep=clock.sleep, uniform=lambda *_: 0)
    expired_provider_cooldown(db, "tencent", clock)

    from stock_analysis.domains.stocks.sources import fetch_price_history
    with routed_stock_requests(db, batch, "tencent", "prices", collector=collector):
        with create_data_session() as routed:
            rows = fetch_price_history("600519", routed, "qfq", start="2026-01-01", end="2026-01-31")
    assert rows[0]["date"] == "2026-01-30"
    assert collector.state("tencent")["cooldown_until"] is None
    assert collector.requests == 1


def test_expired_tencent_qfq_day_fallback_defers_recovery_until_raw_hfq_match(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:17", "stock.prices", "tencent", domain="stock")
    day = [["2026-01-30", "10", "10", "11", "9", "100"]]
    payloads = [{"code": 0, "data": {"sh600519": {"day": day}}} for _ in range(3)]
    clock = Clock()
    collector = RequestCollector(db, session=SequenceSession([json_response(p) for p in payloads]),
                                 clock=clock.time, sleep=clock.sleep, uniform=lambda *_: 0)
    expired_provider_cooldown(db, "tencent", clock)

    from stock_analysis.domains.stocks.sources import fetch_price_history
    with routed_stock_requests(db, batch, "tencent", "prices", collector=collector):
        with create_data_session() as routed:
            rows = fetch_price_history("600519", routed, "qfq", start="2026-01-01", end="2026-01-31")
    assert rows[0]["source_basis"] == "tencent-current-ohlc-verified-unadjusted"
    assert collector.requests == 3
    assert collector.state("tencent")["cooldown_until"] is None
    with db.connection() as conn:
        budget = json.loads(conn.execute("SELECT budget_json FROM collection_provider_state "
                                         "WHERE source_system='tencent'").fetchone()[0])
    assert budget["count"] == 3


def test_expired_tencent_qfq_fallback_mismatch_keeps_provider_blocked(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:18", "stock.prices", "tencent", domain="stock")
    qfq_day = [["2026-01-30", "10", "10", "11", "9", "100"]]
    mismatch = [["2026-01-30", "11", "11", "12", "10", "100"]]
    payloads = [qfq_day, qfq_day, mismatch]
    session = SequenceSession([json_response({"code": 0, "data": {"sh600519": {"day": day}}})
                               for day in payloads])
    clock = Clock()
    collector = RequestCollector(db, session=session, clock=clock.time, sleep=clock.sleep,
                                 uniform=lambda *_: 0)
    expired_provider_cooldown(db, "tencent", clock)

    from stock_analysis.domains.stocks.sources import fetch_price_history
    with routed_stock_requests(db, batch, "tencent", "prices", collector=collector):
        with create_data_session() as routed:
            try:
                fetch_price_history("600519", routed, "qfq", start="2026-01-01", end="2026-01-31")
            except ValueError:
                pass
            else:
                raise AssertionError("Tencent qfq fallback accepted mismatched raw/hfq windows")
    state = collector.state("tencent")
    assert state["cooldown_until"] is not None
    assert state["reason"].startswith("恢复验证失败")
    assert collector.requests == 3


def test_another_collector_cannot_clear_deferred_tencent_recovery(tmp_path):
    db = Database(tmp_path / "stock.sqlite3")
    db.initialize()
    batch = begin_batch(db, "sync_runs:22", "stock.prices", "tencent", domain="stock")
    response = json_response({"code": 0, "data": {}})
    session = Session(response)
    clock = Clock()
    collector = RequestCollector(db, session=session, clock=clock.time, sleep=clock.sleep,
                                 uniform=lambda *_: 0)
    expired = datetime.fromtimestamp(clock.time() - 1, timezone.utc).isoformat()
    with db.connection(write=True) as conn:
        conn.execute("INSERT INTO collection_provider_state(source_system,cooldown_until,reason,denials,budget_json,updated_at) "
                     "VALUES('tencent',?,'recovery_pending:other-owner',1,'{}',?)",
                     (expired, datetime.now(timezone.utc).isoformat()))

    try:
        collector.get(batch, "tencent", "stock.prices", "https://web.ifzq.gtimg.cn/api",
                      {"param": "sh600519,day,,2026-01-31,31,"}, evidence_mode="metadata_only")
    except ProviderBlocked:
        pass
    else:
        raise AssertionError("a second collector bypassed the active recovery verification")
    assert session.calls == 0
    assert collector.requests == 0
