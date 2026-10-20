# 进度

- 已核对20个缺值日期及BigQuant无冷却，已有一条amount读取权限探测成功。
- 正在使用主机现有SDK下载，当前尚未报告完成。

- 已完成主机下载：20个交易日，110861行、16字段，文件存指定bigquant子目录。SELECT *无证券过滤，逐日独立计数与代码去重/amount及volume非空数一致，零重复键，全部文件/来源双哈希校验通过。
- Parquet原生类型、schema、逐列缺值及逐日统计保留；未独立核验历史证券目录覆盖，不宣称未知范围完整。
- manifest.json与README.md提供范围、字段、哈希和source/batch引用；正式验收报告data/verification/reports/20261017_bigquant_congestion_full_market_download.json。
- 未计算拥挤度、未写业务事实、未改后台或日程。下载用一次性脚本完成后清理，公共SDK及已有工具保留。
