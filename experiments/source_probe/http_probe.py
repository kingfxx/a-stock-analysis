"""Bounded HTTP probes of candidate public data sources; no bulk downloads."""

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

SYMBOLS = {"600519": "sh", "600036": "sh", "000001": "sz"}
OUTPUT = Path(__file__).with_name("results") / "http_probe.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; local-research-probe/1.0)"}


def fetch(url, params=None, method="GET", headers=None):
    try:
        response = requests.request(method, url, params=params, headers=headers or HEADERS, timeout=10)
        output = {
            "ok": response.ok,
            "status": response.status_code,
            "url": response.url,
            "content_type": response.headers.get("Content-Type"),
            "content_length": response.headers.get("Content-Length"),
            "body_bytes": len(response.content),
        }
        if method == "GET":
            try:
                if "sina.cn" in url:
                    response.encoding = "utf-8"
                body = response.json()
                output["json_top_keys"] = list(body) if isinstance(body, dict) else []
                output["json"] = body
            except (ValueError, requests.exceptions.JSONDecodeError):
                codec = "gbk" if "gtimg.cn" in url else "utf-8"
                output["sample"] = response.content[:300].decode(codec, errors="replace")
        return output
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:300]}


def main():
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "symbols": {}}
    for symbol, prefix in SYMBOLS.items():
        result["symbols"][symbol] = {
            "baidu_daily": fetch(
                "https://finance.pae.baidu.com/selfselect/getstockquotation",
                {"all": "1", "isIndex": "false", "isBk": "false", "isBlock": "false",
                 "isFutures": "false", "isStock": "true", "newFormat": "1",
                 "group": "quotation_kline_ab", "finClientType": "pc", "code": symbol,
                 "start_time": "", "ktype": "1"},
                headers={**HEADERS, "Accept": "application/vnd.finance-web.v1+json",
                         "Origin": "https://gushitong.baidu.com",
                         "Referer": "https://gushitong.baidu.com/"},
            ),
            "sina_income": fetch(
                "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022",
                {"paperCode": f"{prefix}{symbol}", "source": "lrb", "type": "0", "page": "1", "num": "8"},
            ),
            "tencent_quote": fetch(f"https://qt.gtimg.cn/q={prefix}{symbol}"),
            "tencent_daily": fetch(
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                {"param": f"{prefix}{symbol},day,,,320,qfq"},
            ),
        }
    result["tdx_vipdata_head"] = fetch("https://www.tdx.com.cn/article/vipdata.html", method="HEAD")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {OUTPUT}")
    for symbol, entries in result["symbols"].items():
        print(symbol, {key: (value.get("status") if value["ok"] else value.get("error_type", value.get("status"))) for key, value in entries.items()})
    print("tdx_vipdata_head", result["tdx_vipdata_head"].get("status", result["tdx_vipdata_head"].get("error_type")))


if __name__ == "__main__":
    main()
