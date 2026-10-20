# 进展

已只读定位两个缺值断点和 2024 年报错配，公告列表已确认首次披露日。

已完成最小代码修复：`FundamentalService.read()` 应用披露日覆盖时设置派生标记，`normalize_report_dates()` 对有标记的报告保留覆盖值（含 null），仍保留原 `source_publish_date`。新增隔离数据库集成测试覆盖 `read → normalize → financial_prices → build_period_rows`、重复校正、普通新浪移位及人工日期／null 覆盖；同步更新企业经营趋势指标说明。

验证：`pytest -q tests/test_sources.py tests/test_fundamental_service.py::test_publish_date_override_survives_cached_projection_and_normalization tests/test_fundamental_service.py::test_null_publish_date_override_and_regular_sina_correction_are_stable`，8 passed。曾尝试运行整个 `test_fundamental_service.py`，其中既有 `service.update()` 用例在隔离环境写入 pytest 临时目录备份时触发 Windows `PermissionError`（SQLite 备份文件临时名 rename 被拒）；目标新增集成测试及来源归一化测试均通过。未写正式数据库、未重启后台、未提交。代码与文档 diff 已复核。

Sol 审查与正式验收完成：代码改动仅覆盖优先级、保留来源原值；全套 tests/test_fundamental_service.py tests/test_sources.py 使用仓库内隔离目录测试，19 passed，git diff --check 通过。公告原文与哈希归档完成。正式库备份后写入三项 publish_date 人工覆盖，281 条原始财报未改。宿主机 8765 身份核对、重启和页面实际嵌入数据验收通过；2022/2023/2024 年报分别匹配 2023-03-24/2024-03-26/2025-03-26，前复权价格 19.95/29.60/32.40。审计报告见 data/verification/audits/002463_annual_disclosure_20261014.md。后台保持运行。
