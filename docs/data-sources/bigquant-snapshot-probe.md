# BigQuant 历史交易快照样本测试

2026-10-14 使用项目已有 BigQuant SDK 及配置凭据测试 `cn_stock_real_bar1d`、`cn_stock_shares`、`cn_stock_capital`。访问窗口由用户说明为 2026-10-17 之后不可用；本次成功访问不表示已核验账号的精确到期时间。

2026-10-17 后续日行情全字段下载的实际范围、Arrow 归档读取及配额停止情况，见 [未复权日行情全字段历史归档](bigquant-daily-history-arrow.md)。该次为部分完成，不改写本文的历史样本结论。

## 范围及证据

样本为平安银行、万科 A、京东方 A、浦发银行、上港集团、中国石化、南方航空、江苏银行，覆盖普通 A 股、A/B、A/H 及期间新上市公司。日期为 2015—2017 十二个实际季末交易日及 2018-03-30 核对日。

- 可复用本机工具：`data/verification/tools/probe_bigquant_snapshot.py`，使用 `data/verification/tools/bigquant-runtime/Scripts/python.exe`。工具与证据位于 Git 忽略目录，迁移时需单独保留。
- SDK schema、SQL、日期分区参数、结果及来源引用：`data/verification/reports/bigquant-snapshot-probe-20261014.json`。
- 逐股票/日期计算与本地对照：`data/verification/reports/bigquant-snapshot-probe-20261014-comparison.json`。
- 公共来源数据集 `market.trade_snapshot.sdk_probe`，提供方 `bigquant`；公共原文目录 `data/collection_sources/bigquant/market.trade_snapshot.sdk_probe/`。证据为 SDK 物化结果 JSON，不冒称 HTTP 原始响应；哈希依据登记为 `sdk_materialized_json / gzip_file`。

本次只写公共采集元数据、预算及来源证据，未写交易快照、价格、市值、财务业务表，也未重启后台。通过公共 provider lock/state 串行调度 SDK 操作，默认间隔 10—15 秒，保留跨进程预算；SDK 内部认证/Arrow Flight 的细分请求未逐一追踪，因此批量正式接入仍需补齐传输层计数和归档适配，不能宣称该探测已完成正式采集器接入。首次受限环境初始化无返回，手动中断；允许外网的运行成功完成。

## 已验证结果

行情和日频股本各返回 98 行，事件股本返回 289 行。8×13 的 104 个组合中，江苏银行上市前六期没有行，符合其上市时点；不将上市前日期当缺口。这里只验证样本，不证明历史全市场覆盖完整。

1. 行情 `close` 为未复权收盘价；有值的 95 行与本地同日季度价格全部一致。`change_ratio` 为小数比例，东财 `change_rate` 为百分数，转换为 `change_ratio * 100`；有有效收盘价/昨收时与 `close/pre_close-1` 的最大差约为 4.85e-11。2018 年八个东财重叠样本支持该转换。
2. `cn_stock_shares.total_shares` 样本单位为股，`a_float_shares` 为流通 A 股，不能用 `free_float_shares` 替代。2018 年八个样本的总股本、流通 A 股均与东财 `total_shares/free_shares_a` 一致。不同股票的映射仍需正式重叠校验。
3. 未复权价乘日频总股本得到 95 个可计算市值，其中 68 个具备有效旧市值对照，全部在当前快照校验阈值 `max(1元, 市值×1e-6)` 内一致；其中包括八个 2018 年东财原始快照对照。A/H、A/B 样本继续使用总股本乘 A 股价格口径，非跨市场分项估值之和。
4. `cn_stock_capital` 按 `change_date <= trade_date` 的最近事件匹配，样本不存在最近变动日总股本冲突。97/98 的总股本与日频表一致，但万科 2015-03-31 日频总股本 11,046,108,081 股，最近事件为 2015-02-28 的 11,045,647,571 股，相差 460,510 股。流通 A 股另有万科 2016-03-31 差异；不能无条件替换。
5. 万科 2015-12-31、2016-03-31、2016-06-30 的行情行存在，`close` 为空、`pre_close` 有值、`change_ratio=0`。缺收盘价不能转为零或沿用昨收生成季末市值；零涨跌幅保留为来源值，但不当作该日有效价格证明。

## 对历史补齐方案的建议（尚未实施）

- 同交易日股本优先 `cn_stock_shares`；缺项时才以 `cn_stock_capital` 最近有效事件作候选回退，记录变动/公告日期、单位、版本和冲突检查。
- 新增可补字段：行情来源的历史证券简称、`close_price`、`change_rate`；日频股本的 `total_shares`，经定义验证后的 `free_shares_a`；可计算总市值及流通 A 股市值。
- 有效旧总市值优先保留；新计算值用于补缺和交叉校验，差异进入隔离清单。PE/PB 仍按行业页已实现公式计算，使用独立合成定义版本。
- 正式批量采集前核验全市场名单有效期及逐字段覆盖、SDK 返回截断与分页机制；在用户提供的访问截止日前完成必要的历史来源缓存。样本成功不授权自动执行全市场采集或入库。

官方字段说明：[未复权日行情](https://bigquant.com/data/datasources/cn_stock_real_bar1d)、[日频股本](https://bigquant.com/data/datasources/cn_stock_shares)、[股本事件](https://bigquant.com/data/datasources/cn_stock_capital)。
