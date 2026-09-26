"""Verify observed source agreement on the recorded samples without network access."""

import json
from decimal import Decimal
from pathlib import Path

RESULTS = Path(__file__).with_name("results")


def read(name):
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


def main():
    tdx = read("tdx_package_probe.json")["samples"]
    tencent = read("detail_probe.json")["symbols"]
    checked = 0
    for code, market in (("600519", "sh"), ("600036", "sh"), ("000001", "sz")):
        day = tdx[market + code]
        row = tencent[code]["tencent_unadjusted_320"]["last"]
        assert day["date"] == row[0] == "2026-09-24"
        for field, index in (("open", 1), ("close", 2), ("high", 3), ("low", 4)):
            assert Decimal(str(day[field])) == Decimal(row[index]), (code, field)
            checked += 1
        assert abs(day["volume_shares"] - int(Decimal(row[5]) * 100)) < 100, code
        checked += 1
    sina = read("detail_probe.json")["symbols"]["600519"]
    income = sina["sina_income_120"]["latest_fields"]
    balance = sina["sina_balance_120"]["latest_fields"]
    assert Decimal(income["营业收入"]) == Decimal("90703260964.48")
    assert Decimal(income["归属于母公司所有者的净利润"]) == Decimal("44516880421.86")
    assert Decimal(balance["归属于母公司股东权益合计"]) == Decimal("251253594419.50")
    checked += 3
    for code, expected_revenue, expected_profit in (
        ("600036", "178181000000", "76445000000"),
        ("000001", "70617000000", "25696000000"),
    ):
        income = read("detail_probe.json")["symbols"][code]["sina_income_120"]["latest_fields"]
        assert Decimal(income["营业收入"]) == Decimal(expected_revenue)
        assert Decimal(income["归属于母公司的净利润"]) == Decimal(expected_profit)
        checked += 2
    print(f"verified {checked} recorded field comparisons against independent sources")


if __name__ == "__main__":
    main()
