"""Pure disclosure-date rules for structured CNINFO metadata; no network or DB access."""

from datetime import datetime
from zoneinfo import ZoneInfo

from stock_analysis.domains.stocks.disclosure_dates import apply_disclosure_dates, choose, classify
from stock_analysis.domains.stocks.sources import normalize_report_dates


def _item(title, day, key="a1", **extra):
    local = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return {"announcementId": key, "announcementTitle": title,
            "announcementTime": int(local.timestamp() * 1000),
            "adjunctUrl": f"finalpage/2025/{key}.PDF", "secCode": "002463", **extra}


def _classified(title, day, key="a1", **extra):
    return classify(_item(title, day, key, **extra), code="002463")


def test_revised_summary_does_not_replace_original_disclosure_date():
    original = _classified("2024年度报告", "2025-03-26", "original")
    summary = _classified("2024年度报告摘要", "2025-03-26", "summary")
    revised_summary = _classified("2024年度报告摘要（更正后）", "2025-11-28", "revised")
    selected = choose([{"period": "2024-12-31"}], [original, summary, revised_summary])["2024-12-31"]
    assert selected["status"] == "matched_original"
    assert selected["first_published_on"] == "2025-03-26"
    assert selected["latest_revision_on"] == "2025-11-28"


def test_unpunctuated_issuer_prefix_uses_response_stock_name():
    item = classify(_item("中远海控2024年年度报告", "2025-03-22", secName="中远海控"), code="002463")
    assert item["revision_marker"] == "original"
    unknown = classify(_item("关于2024年年度报告", "2025-03-22", secName="中远海控"), code="002463")
    assert unknown["revision_marker"] == "unknown"


def test_unconfirmed_title_keeps_sina_candidate_unverified():
    summary = _classified("2024年年度报告摘要", "2025-03-26", "summary")
    decision = choose([{"period": "2024-12-31"}], [summary])["2024-12-31"]
    rows = apply_disclosure_dates([{"period": "2024-12-31", "publish_date": "2025-03-26"}],
                                 {"dates": {"2024-12-31": {**decision, "candidates": [1]}}})
    assert rows[0]["publish_date"] == "2025-03-26"
    assert rows[0]["publish_date_source"] == "sina_unverified"


def test_original_formal_titles_with_company_prefix_cover_all_periods():
    cases = [
        ("沪电股份：2024年年度报告全文", "2024-12-31", "annual"),
        ("中远海控：2024年半年度报告全文", "2024-06-30", "interim"),
        ("沪电股份：2025年第一季度报告", "2025-03-31", "q1"),
        ("沪电股份：2024年第三季度报告", "2024-09-30", "q3"),
    ]
    for title, period, report_type in cases:
        item = _classified(title, "2025-04-30")
        assert item["periods"] == [period]
        assert item["report_type"] == report_type
        assert item["title_category"] == "formal_report"
        assert item["revision_marker"] == "original"


def test_revisions_summaries_and_multi_year_correction_notice_are_distinct():
    revised = _classified("沪电股份：2024年年度报告（修订版）", "2025-05-08", "r1")
    summary = _classified("沪电股份：2024年年度报告摘要", "2025-04-30", "s1")
    correction = _classified("关于2022年及2023年年度报告更正的说明", "2025-05-08", "c1")
    assert revised["title_category"] == "formal_report"
    assert revised["revision_marker"] == "revision"
    assert summary["title_category"] == "summary"
    assert summary["revision_marker"] != "original"
    assert correction["title_category"] == "correction_notice"
    assert correction["periods"] == ["2022-12-31", "2023-12-31"]


def test_non_report_titles_do_not_become_original_reports():
    titles = ["沪电股份：2024年年度股东大会决议公告",
              "沪电股份：2024年度审计报告",
              "沪电股份：2024年年度报告（英文版）"]
    for title in titles:
        item = _classified(title, "2025-04-30")
        assert item["title_category"] != "formal_report"
        assert item["revision_marker"] != "original"


def test_summary_date_conflict_date_conflict_flag_and_incomplete_range_stay_pending():
    original = _classified("沪电股份：2024年年度报告全文", "2025-04-30", "o1")
    summary = _classified("沪电股份：2024年年度报告摘要", "2025-05-01", "s1")
    report = [{"period": "2024-12-31", "publish_date": "2025-04-30"}]
    decision = choose(report, [original, summary])["2024-12-31"]
    assert decision["status"] == "pending"
    assert decision["first_published_on"] is None

    conflicted = {**original, "date_conflict": True}
    decision = choose(report, [conflicted])["2024-12-31"]
    assert decision["status"] == "pending"
    assert decision["first_published_on"] is None

    decision = choose(report, [original], complete=False)["2024-12-31"]
    assert decision["status"] == "pending"
    assert decision["first_published_on"] is None


def test_revision_only_never_supplies_first_disclosure_or_price_date():
    revised = _classified("沪电股份：2024年年度报告（修订版）", "2025-05-08", "r1")
    report = [{"period": "2024-12-31", "publish_date": "2025-04-30"}]
    decision = choose(report, [revised])["2024-12-31"]
    applied = apply_disclosure_dates(report, {"dates": {"2024-12-31": decision}})[0]
    assert decision["latest_revision_on"] == "2025-05-08"
    assert applied["publish_date"] is None
    assert applied["publish_date_source"] == "none"
    assert normalize_report_dates([applied])[0]["publish_date"] is None


def test_cancelled_original_revision_and_summary_confirm_first_date_only_with_complete_same_day_evidence():
    period = "2020-12-31"
    report = [{"period": period}]
    cancelled = _classified("沪电股份：2020年年度报告（已取消）", "2021-03-25", "cancelled")
    revision = _classified("沪电股份：2020年年度报告（更新后）", "2021-03-25", "revision")
    summary = _classified("沪电股份：2020年年度报告摘要", "2021-03-25", "summary")
    assert cancelled["revision_marker"] == "cancelled_original"
    assert revision["revision_marker"] == "revision"
    selected = choose(report, [cancelled, revision, summary])[period]
    assert selected["status"] == "matched_original"
    assert selected["first_published_on"] == "2021-03-25"
    assert selected["selected_first_evidence"] == "cancelled"
    assert "三项发布时间一致" in selected["reason"]

    for candidates in (
        [cancelled, {**revision, "published_on": "2021-03-26"}, summary],
        [cancelled, revision],
        [cancelled, revision, {**summary, "published_on": "2021-03-26"}],
        [{**cancelled, "date_conflict": True}, revision, summary],
    ):
        decision = choose(report, candidates)[period]
        assert decision["first_published_on"] is None
        assert decision["status"] == "pending"
    incomplete = choose([{**report[0], "_query_complete": False}],
                        [cancelled, revision, summary])[period]
    assert incomplete["status"] == "pending"
    assert incomplete["first_published_on"] is None


def test_cancelled_title_is_not_generalized_to_an_original_without_explicit_marker():
    cancelled = _classified("沪电股份：2020年年度报告", "2021-03-25", "plain")
    assert cancelled["revision_marker"] == "original"
    explicit = _classified("沪电股份：2020年年度报告（已取消）", "2021-03-25", "cancelled")
    assert explicit["revision_marker"] == "cancelled_original"


def test_retained_conflict_is_idempotent_and_normalization_preserves_final_date():
    report = {"period": "2024-12-31", "publish_date": "2025-03-26",
              "source_publish_date": "2026-03-26"}
    first = apply_disclosure_dates([report], {"dates": {"2024-12-31": {
        "id": 7, "first_published_on": "2025-03-26", "status": "matched_original",
        "reason": "旧的已核验日期", "rule_version": "test-v1"}}})[0]
    conflict_data = {"dates": {"2024-12-31": {
        "id": 8, "first_published_on": "2025-03-26", "status": "conflict_retained",
        "reason": "新候选冲突，保留旧值", "rule_version": "test-v1"}}}
    retained = apply_disclosure_dates([first], conflict_data)[0]
    repeated = apply_disclosure_dates([retained], conflict_data)[0]
    assert retained["publish_date"] == repeated["publish_date"] == "2025-03-26"
    assert retained["publish_date_source"] == "cninfo"
    assert normalize_report_dates([retained]) == [retained]
    assert normalize_report_dates(normalize_report_dates([retained])) == [retained]

def test_directional_earnings_forecasts_are_not_formal_reports():
    from stock_analysis.domains.stocks.disclosure_dates import classify, is_earnings_notice
    from datetime import datetime
    for suffix in ('预增', '预减', '预盈', '预亏', '预告', '快报'):
        title = '中远海控2024年度业绩' + suffix + '公告'
        assert is_earnings_notice(title)
        row = classify({'announcementId': suffix, 'announcementTitle': title,
                        'announcementTime': int(datetime.fromisoformat('2025-01-11T08:00:00+08:00').timestamp()*1000),
                        'secCode': '601919', 'adjunctUrl': 'notice.pdf'}, '601919')
        assert row['title_category'] == 'forecast'
        assert row['report_type'] is None
    assert not is_earnings_notice('中远海控2024年年度报告')
    assert not is_earnings_notice('关于预计利润分配的公告')
