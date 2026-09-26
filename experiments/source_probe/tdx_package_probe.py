"""Inspect one official TDX daily package and retain only selected stock rows."""

import io
import json
import struct
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

DAY = "20260924"
SYMBOLS = {"sh600519", "sh600036", "sz000001"}
URL = f"https://www.tdx.com.cn/products/data/data/g4day/{DAY}.zip"
OUTPUT = Path(__file__).with_name("results") / "tdx_package_probe.json"


def main():
    r = requests.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    if not r.content.startswith(b"PK"):
        raise ValueError("Response is not a ZIP archive")
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "url": URL,
              "download_bytes": len(r.content), "markets": {}, "samples": {}}
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        names = archive.namelist()
        result["members"] = names
        for market in ("sh", "sz", "bj"):
            cod_name, md1_name = f"{market}{DAY[2:]}.cod", f"{market}{DAY[2:]}.md1"
            cod, md1 = archive.read(cod_name), archive.read(md1_name)
            if len(cod) % 150 or len(md1) % 512 or len(cod) // 150 != len(md1) // 512:
                raise ValueError(f"{market}: invalid record sizes")
            result["markets"][market] = {"records": len(cod) // 150,
                                         "code_bytes": len(cod), "data_bytes": len(md1)}
            for offset in range(0, len(cod), 150):
                record = cod[offset:offset + 150]
                code = record[:6].decode("ascii")
                symbol = market + code
                if symbol not in SYMBOLS:
                    continue
                sequence = struct.unpack("<H", record[32:34])[0]
                block = md1[sequence * 512:(sequence + 1) * 512]
                if len(block) != 512:
                    raise ValueError(f"{symbol}: missing data block")
                open_, high, low, close = struct.unpack("<4d", block[12:44])
                result["samples"][symbol] = {
                    "date": f"{DAY[:4]}-{DAY[4:6]}-{DAY[6:]}",
                    "prev_close": struct.unpack("<d", block[4:12])[0],
                    "open": open_, "high": high, "low": low, "close": close,
                    "volume_shares": struct.unpack("<Q", block[56:64])[0],
                    "amount_yuan": struct.unpack("<d", block[72:80])[0],
                }
    if set(result["samples"]) != SYMBOLS:
        raise ValueError(f"missing symbols: {SYMBOLS - set(result['samples'])}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {OUTPUT}; package bytes={len(r.content)}")
    for symbol, row in result["samples"].items():
        print(symbol, row)


if __name__ == "__main__":
    main()
