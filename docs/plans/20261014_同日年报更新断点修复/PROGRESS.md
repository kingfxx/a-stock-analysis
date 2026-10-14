# 进展

## 验收完成

Sol审查实现及相关调用路径，扩展运行披露日期、公告服务、元数据、后台集成及财务服务测试，46项通过。正式库刷新前备份至 `data/backups/pre-same-day-disclosure-20261014.sqlite3`，仅刷新沪电股份公告元数据，281条财务原始记录及内容哈希保持不变。

8765已重启（进程30436），启动日志结构版本33。正式页面HTTP 200，2020年Q4返回首次披露日2021-03-25、前复权价13.20元及对应交易日2021-03-25，规则版本cninfo-title-v4。证据与验收结果保存在 `data/verification/reports/same_day_disclosure_fix_20261014.json`；后台日志保存在 `data/verification/logs/daily-8765-same-day-20261014.out.log` 与同名err.log。后台保持运行。

Luna完成：披露规则升级至 `cninfo-title-v4`；新增显式“已取消”旧版标记，仅在完整查询范围或三项均已验证缓存中，股票/报告期匹配的取消原版、正式修订版及非修订摘要日期唯一一致且无冲突时确认首次日期。服务发布保留取消原版元数据行作为 `original_announcement_row_id`，修订公告仍写入 `revision_announcement_row_id`。未将取消公告视为当前财报。

补充测试覆盖三项同日成功、跨日、缺项、摘要日期冲突、date_conflict、不完整范围、普通原版不退化和数据库引用关系。`pytest --basetemp .t tests/test_disclosure_dates.py tests/test_announcement_service.py -q`：19 passed；临时目录已清理。未触碰正式库、采集或后台。此前第一次测试因隔离目录权限未通过，改用仓库内短路径 `.t` 后通过。
