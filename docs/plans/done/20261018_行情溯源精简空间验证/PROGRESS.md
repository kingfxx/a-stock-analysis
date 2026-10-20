# 进度

已建立隔离验证计划，准备在线副本和可逆精简试验。

隔离试验完成，耗时65.11秒。全部353116行编码/解码和非溯源列指纹核对通过，335163个估算完整还原，0回退。JSON文本614.56→64.20MiB，VACUUM后行情表1308.51→151.79MiB、索引29.56MiB、全库1382.10MiB。正式库/应用代码/8765未修改；临时副本已删除。报告data/verification/reports/20261018_lineage_space_probe.md及.json，工具data/verification/tools/probe_index_price_lineage_compaction.py保留。尚未授权正式实现和部署。

## 追加验证

开始第二轮更紧凑结构的空间实测；仅隔离副本，不部署。

## 第二轮完成

71.735秒完成4组隔离实测；完整方案均保留353116行全部数值及溯源，原列/旧溯源JSON逐行还原指纹通过，0回退。位置JSON方案表109.02MiB、索引14.45MiB、合计123.46MiB；进一步规范化表72.48MiB、索引9.00MiB、合计81.48MiB。后者建议作为后续实现方向；正式库及8765未改，临时库已清理。详见data/verification/reports/20261018_lineage_compact_variants.md及.json。
