# 个股财务 JSON 无损压缩

Schema v35，仅涉及 `financial_reports.raw_json`。不新增表，不更改金额口径、来源、报告期、披露日期、修订时间、`content_hash`、`obtained_at` 或 `run_id`。

## 编码与读取

- 现有列保留 TEXT 亲和性，物理写入为 SQLite BLOB。普通数据库浏览器看到二进制，不能直接 `json_extract(raw_json,...)`；必须先用协议解码。旧纯文本继续可读。
- 每条独立编码：6 bytes `ASTKFR` 加版本 byte `01`（整体 magic 为 `b'ASTKFR\x01'`，共 7 bytes），8 bytes 大端未压缩长度，32 bytes 原文 SHA256，最后为 `zlib.compress(UTF-8, level=6)` 的完整流。
- 原始文本保持逐字节一致；新采集仍先按项目原规则生成 canonical JSON 及 `content_hash`，再压缩。内嵌哈希和行内 content_hash 都针对原始 UTF-8，不针对压缩流。
- `stock_analysis.domains.financials.financial_storage` 集中编解码。解码限制 64 MiB、验证长度、完整流、无尾随数据及 SHA256，不静默回退损坏 BLOB。
- 仓储 `Database.financial_reports()` 返回解压后的原 TEXT。三表、DCF/WACC、旧行业适配器、研究快照、Checklist 与清理预览的直接 SQL 读取入口显式调用统一解码，不在全库行工厂增加额外操作。研究快照依然嵌入原 JSON 文本。
- `.agents/skills/a-share-value-research/scripts/research.py` 保持独立标准库读取，支持相同协议；交叉兼容性由测试覆盖。其他外部 SQL 工具需主动适配协议。

## 历史迁移与故障恢复

初始化按原流程先生成完整升级前 SQLite 备份，随后在同一 schema 事务执行 v35 Python hook：逐条校验已有 content_hash、压缩、解压复核，仅更新 raw_json。任意错误回滚全部修改及 schema 版本；拒绝将不一致原文当作正常来源。原主键、索引、触发器和来源关系保留。

初始化成功后再次启动不会重复迁移。人工直接写入的旧 TEXT 仍可读取；应用新写入始终为压缩 BLOB。`Database.check` 和备份校验会检查压缩流及其与 content_hash 的一致性。备份仍只需包含同一 SQLite，未增加外部载荷文件；旧 v34 备份恢复后沿用升级流程。

日常库迁移必须核对并停止原后台，取得实例锁，确认无运行任务。迁移后逐条比对还原原文及其他列、核对其他表摘要；再通过已有维护空间整理回收空闲页，并检查完整性和外键。最后在宿主机重启原 8765，检查启动日志及只读接口。整理不删除正式备份；原备份继续占磁盘。

## 验证入口

- 编解码、损坏拒绝、v34 历史迁移/回滚、备份恢复及技能协议兼容：`tests/test_financial_storage.py`。
- 财务页面/三表/DCF/Checklist/研究快照/清理/维护：原相关测试套件；使用隔离临时数据库。
- 本任务计划：`docs/plans/20261017_financial_reports无损压缩/`。
- 初始研究：`data/verification/reports/financial_reports_storage_20261017/ANALYSIS.md`；正式迁移及接口验收保存在同目录的 `live_migration.json`、`http_after.json`、`wacc_offline.json` 和 `VALIDATION.md`。
- 可复用工具：`data/verification/tools/migrate_financial_reports.py` 支持 `--rehearse` 完整副本演练和 `--apply` 正式维护；`check_financial_compression_http.py` 比较三表/DCF只读接口，WACC由 `check_financial_compression_wacc.py` 固定独立利率输入后离线核对，避免校验期间访问利率来源。
- 当前后台日志：`data/verification/logs/daily-8765-financial-compression-20261017.out.log` 与同名 `.err.log`。
