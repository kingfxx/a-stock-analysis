"""Offline regression for the observed P0 source contracts, without network calls."""

import json
from datetime import date
from pathlib import Path

import pytest

from experiments.source_probe.storage_upgrade_probe import financing_window
from quarterly_dashboard.chips import chip_rows
from quarterly_dashboard.sources import parse_daily_prices, symbol_for

SAMPLES = json.loads((Path(__file__).resolve().parents[1] / "docs" / "data-sources" /
                      "storage-upgrade-p0-samples.json").read_text(encoding="utf-8"))


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class Session:
    def __init__(self, payload):
        self.payload = payload
        self.params = None

    def get(self, url, params, **kwargs):
        self.params = params
        return Response(self.payload)


@pytest.mark.parametrize("code", ["601919", "600887", "000001"])
def test_recorded_financing_samples_keep_raw_units_and_real_required_fields(code):
    sample = SAMPLES["stocks"][code]["financing"]
    rows = chip_rows([sample["sample"]], "financing")
    assert rows[0]["margin_balance"] == sample["sample"]["RZYE"]
    assert rows[0]["net_buy"] == sample["sample"]["RZJME"]
    assert sample["missing_known_dates"] == []
    assert sample["changed_core_dates_during_probe"] == []
    assert sample["window_count"] < sample["full_count"]
    assert "RQMCL" in sample["all_source_fields"]


def test_financing_date_filter_contract_rejects_ignored_bounds_and_missing_pages():
    record = SAMPLES["stocks"]["601919"]["financing"]["sample"]
    day = record["DATE"][:10]
    payload = {"success": True, "result": {"count": 1, "pages": 1, "data": [record]}}
    session = Session(payload)
    assert financing_window("601919", session, day, day) == [record]
    assert session.params["filter"] == f'(SCODE="601919")(DATE>=\'{day}\')(DATE<=\'{day}\')'
    with pytest.raises(ValueError, match="ignored"):
        financing_window("601919", session, "2000-01-01", "2000-01-02")
    session = Session({"success": True, "result": {"count": 2, "pages": 2, "data": [record]}})
    with pytest.raises(ValueError, match="incomplete"):
        financing_window("601919", session, day, day)


@pytest.mark.parametrize("code", ["601919", "600887", "000001"])
def test_qfq_recorded_pages_keep_source_precision_and_overlap_limitation(code):
    sample = SAMPLES["stocks"][code]["qfq_overlap"]
    symbol = symbol_for(code)
    raw = [sample["current_first"], sample["current_last"]]
    rows = parse_daily_prices({"code": 0, "data": {symbol: {"qfqday": raw}}}, symbol, "qfq")
    assert rows[0]["close"] == float(raw[0][2])
    assert date.fromisoformat(rows[0]["date"]) < date.fromisoformat(rows[-1]["date"])
    assert sample["overlap_count"] >= 20
    assert sample["consistent_in_this_sample"] is True
    assert sample["covers_full_history"] is False


def test_ah_holder_source_does_not_imply_matching_scope_or_complete_detail_history():
    sources = SAMPLES["stocks"]["601919"]["shareholders"]
    f10 = sources["RPT_F10_EH_HOLDERNUM"]
    detail = sources["RPT_HOLDERNUM_DET"]
    assert f10["latest"]["HOLDER_TOTAL_NUM"] > 0
    assert detail["latest"]["HOLDER_NUM"] > 0
    assert detail["latest"]["END_DATE"] < f10["latest"]["END_DATE"]
    assert "unverified" in f10["scope"] and "unverified" in detail["scope"]
