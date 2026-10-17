# BigQuant 未复权日行情全字段历史归档

2026-10-17 用户授权下载 `cn_stock_real_bar1d` 全部可访问历史、全市场证券、全部字段，供后续离线分析使用。联网操作在 Windows 宿主机执行，复用现有 SDK 和公共采集调度。

**本次为部分完成，最终状态 `stopped_quota`，不是全历史下载完成。** 未计算拥挤度，未写入行情、交易快照或拥挤度业务事实，也没有新增定时采集或自动续跑。

## 来源范围与实际完成范围

独立逐日计数查询返回 **2005-01-04 至 2026-10-16，共 5,289 个交易日、16,597,171 行**。查询没有证券代码或交易所过滤。该范围是来源本次查询返回范围，不代表独立核实了历史证券名单或来源最早成立日期。

已验收分段如下，季度日期仅列实际有记录的交易日：

| 分段 | 首日 | 末日 | 交易日数 | 行数 | Arrow 来源 ID |
|---|---|---|---:|---:|---:|
| 2005 | 2005-01-04 | 2005-12-30 | 242 | 328,806 | 2896 |
| 2006 | 2006-01-04 | 2006-12-29 | 241 | 328,623 | 2898 |
| 2007 | 2007-01-04 | 2007-12-28 | 242 | 354,216 | 2900 |
| 2008 | 2008-01-02 | 2008-12-31 | 246 | 387,436 | 2902 |
| 2009 | 2009-01-05 | 2009-12-31 | 244 | 394,995 | 2904 |
| 2010 | 2010-01-04 | 2010-12-31 | 242 | 452,708 | 2906 |
| 2026Q3 | 2026-07-01 | 2026-09-30 | 65 | 360,431 | 2916 |
| 2026Q4 | 2026-10-08 | 2026-10-16 | 7 | 39,019 | 2914 |
| 合计 | — | — | 1,529 | 2,646,234 | — |

8 个行情分段的压缩归档共 **112,918,776 字节，约 113 MB**。尚未取得 **3,760 个交易日、13,950,937 行**，涉及 2011—2025 年及 2026 年第一、第二季度。不能将来源可查询范围当成本地已有范围。

最初按年从早期下载；用户追加近期优先要求后，在当时在途年度完成后切换为 **2026 → 2025 → 2024 → ……**。大年度改为季度倒序，已验收的早期年度保留。全年请求失败时最初只得到 `FlightUnavailableError`；补充诊断后确认后续分段实际受到配额限制，因此不能据此断言接口存在固定年度行数上限。

## 文件、数据库记录及校验材料

| 内容 | 项目相对路径 |
|---|---|
| 下载清单 | [manifest.json](../../data/collection_sources/bigquant/cn_stock_real_bar1d_history/manifest.json) |
| 就地读取说明 | [README.md](../../data/collection_sources/bigquant/cn_stock_real_bar1d_history/README.md) |
| 行情正文归档 | `data/collection_sources/bigquant/market.trade_snapshot.bigquant_inputs/*.raw.gz` |
| 最终验收报告 | [20261017_bigquant_full_history_arrow.json](../../data/verification/reports/20261017_bigquant_full_history_arrow.json) |
| 下载工具 | [archive_bigquant_history.py](../../data/verification/tools/archive_bigquant_history.py) |
| 本次计划与进度 | [计划目录](../plans/20261017_BigQuant全历史Arrow归档/PLAN.md) |

本次批量行情正文**仅保留一份 gzip 压缩的 Arrow IPC stream**，没有额外生成 Parquet、CSV 或未压缩 Arrow 副本。它是 SDK 返回结果物化后的 Arrow 归档，不冒称网络数据包原始字节。

归档复用现有 SDK 数据集标识 `market.trade_snapshot.bigquant_inputs`，真实提供方为 `bigquant`；实际来源表、SQL、日期分区及此次用途由公共 batch/source 的范围参数登记。该目录名不表示行情已经写入 `market_trade_snapshot` 业务表。

`manifest.json` 的 `segments` 以分段名索引：只有 `status=verified` 的分段才是已验收数据。`arrow_source_id` 指向实际行情流，`file_path` 相对于项目 `data/` 目录，双 SHA256 对应解压后的 Arrow 和压缩存储文件。`source_ids` 还可能包含 SDK 元数据请求，不能把每个来源文件都作为行情 Arrow 解析。失败请求可能没有正文文件。

公共 `collection_batches`、`collection_sources` 保留请求、范围、时间、状态及引用；`collection_provider_state` 保存跨任务共享预算和冷却。本次审计共 33 条请求记录、25 份有正文的归档，其中仅上述 8 份为已验收的历史行情分段。其余包括认证/查询元数据及逐日计数，不能按文件数量统计行情分段。

**数据文件、运行时及工具在 Git 忽略目录，迁移时须另行复制；单独 SQLite 备份不包含归档文件。** 同时保留数据库、公共来源目录、清单和验收报告，迁移后核验双哈希。

## 字段与离线读取

使用 `SELECT *`，完整保留本次返回的 16 个字段及 Arrow 原生类型：

```text
date, instrument, name, adjust_factor, pre_close, open, close, high, low,
volume, deal_number, amount, change_ratio, turn, upper_limit, lower_limit
```

有效零、负值及来源空值原样保留，不补零、不插值、不使用其他日期替代。金额、成交量、换手率等字段未在归档阶段统一换算单位；业务计算前仍需按对应来源定义核验口径。未复权价格等此前样本核对见 [BigQuant 历史交易快照样本测试](bigquant-snapshot-probe.md)。

从仓库根目录使用带 `pyarrow` 的 Python 执行，按清单读取单段，无需联网或重新初始化 SDK：

```python
import gzip
import json
from pathlib import Path
import pyarrow as pa

root = Path.cwd()
manifest_path = root / "data/collection_sources/bigquant/cn_stock_real_bar1d_history/manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
segment = manifest["segments"]["2026Q3"]
assert segment["status"] == "verified"
archive = root / "data" / segment["file_path"]
with gzip.open(archive, "rb") as stream:
    table = pa.ipc.open_stream(stream).read_all()
# 按需 table.select(["date", "instrument", "amount"]).to_pandas()
```

本机已安装对应运行时：`data/verification/tools/bigquant-runtime/Scripts/python.exe`。建议逐段读取，避免将未来全历史一次性展开到内存。正式使用前以公共来源记录或清单中的双哈希校验归档。

## 校验结论与边界

每个已验收分段均核对 16 字段结构、实际日期、逐日行数、证券去重数、`amount`/`volume` 非空数，并检查 `date + instrument` 主键无空值、无重复。逐日统计与独立计数来源 2894（批次 1350）一致。

最终审计从归档回读 Arrow，验证逐列空值数及分段不重叠；25 份有正文来源的原文/存储双哈希通过，数据库外键检查通过，没有遗留运行中批次。历史证券名单未独立核验，证券总体覆盖保持未知；未下载的日期与已下载数据中的字段空值分别记录。

另有此前专门取得的 20 个缺值日期全市场数据，见 [20 日下载清单](../../data/collection_sources/bigquant/cn_stock_real_bar1d_20260731_20260827/manifest.json)，共 110,861 行。那次保留了 Parquet 及公共 Arrow；这些日期已包含在本次 2026Q3 中，不能再次加到本次行数上，也未删除既有文件。

后续用户授权使用上述20日资料计算拥挤度，并补入本地上证点数close，一次性写入独立自算身份；实施范围及验收见 [20日拥挤度离线补值](market-congestion-gap-backfill.md)。本次全历史归档本身没有发布行情业务事实，后续补值也没有扩展未完成的下载范围。

## 配额停止及后续恢复约束

2026-10-17 来源在 `FlightUnavailableError` 原始诊断中明确返回：

```text
数据配额不足，已使用45309716，本周配额为50000000，当前请求配额为4911296
```

余额为 **4,690,284**，当前 2026Q1 需要 **4,911,296 = 306,956 行 × 16 字段**；说明该请求的额度按字段数据量计算，不能用剩余配额直接当作可下载行数，也不能把数据周配额等同于已核验的积分余额。

确认真实拒绝后立即停止本轮全部同源请求，并登记共享冷却至北京时间 **2026-10-17 23:09:31**。公共调度已补充配额拒绝识别，优先于临时连接重试；相关隔离回归测试 17 项通过。此前只记录异常类名的失败，保留其原状态，不反推未捕获的原始原因。

当前没有自动恢复任务。后续继续前需核实账号实际访问权限、SDK 既有授权窗口、周配额及共享冷却状态；冷却结束不代表配额恢复。现有 SDK 按用户此前给定窗口限制 2026-10-17 之后调用，不能自动延长。本次工具复用已验收段，显式标记失败的分段不会自动重下；若重新获得条件，应另行定向补失败/未取得分段，并保持近期优先及共享限速，不重新下载已验收历史。
