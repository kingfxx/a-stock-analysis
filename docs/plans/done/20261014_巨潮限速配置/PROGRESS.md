# 进展

Sol已核对公共PacingPolicy及GET/invoke入口，默认验证硬编码保守下限，需实现提供方范围的明确例外。

Luna执行：在RequestCollector按provider选择cninfo策略，GET与invoke共用2秒／30次策略及既有公共锁、预算、拒绝冷却。保留其他来源PacingPolicy下限及非cninfo stock_fast行为；provider专属策略不能作为全局policy。报告列出cninfo有效策略。补充隔离数据库假时钟测试及规则/方案策略描述。

验证：`PYTHONPATH=. python -m pytest -q --basetemp=.pytest_cninfo tests/test_collection_provider_pacing.py tests/test_industry_memberships.py` 通过，35 passed。覆盖20次额外休息为0、2秒间隔、30次窗口在GET/invoke及跨collector下生效、stock_fast不绕过cninfo、拒绝冷却/Retry-After/重复拒绝延长，以及其他来源策略。临时测试库已清理；未触及正式数据库、后台或真实来源。

Sol审查完成：提供方专属策略不能作为全局策略，cninfo GET/invoke不受stock_fast绕过，其他来源默认策略保留；无数据库结构变更、未清预算或冷却。主代理隔离回归64 passed，Luna补充及相关回归35 passed（套件有重叠，不合计为99），git diff --check通过。临时库清理完毕。已核验日常8765原进程42056并重启为42080；日志正常、沪电缓存页面HTTP200，后台保持运行。有效配置报告：data/verification/reports/cninfo_pacing_20261014.json。没有真实巨潮请求或自动公告日期采集接入；旧独立链仍未改造。
