# 执行进度

- 已完成：汇总计算、输入签名、审计版本复用/追加、当前季度版本映射。
- 已完成：日常季末快照、历史快照、历史合成、停牌参考价发布流程接入汇总。
- 已完成：行业季度总额、覆盖率和排行市值改为读取汇总行；公司快照数据及共同成分同比继续按原口径读取。
- 已完成：存储、行业数据结构、来源及指标说明和维护表用途已更新，无 schema 迁移。
- 验证：`test_industry_cap_summary.py`、`test_market_trade_snapshot.py`、`test_market_snapshot_composite.py`、`test_market_suspended_price.py` 共 30 项通过；compileall 和 `git diff --check` 通过。
- 已只读核查日常库已有 47 个季度快照（2015Q1—2026Q3）和 18,359 条历史行业汇总；按用户确认保留这些历史数据，不执行 47 期回填。
- 已调整页面读取为按季度优先选择新来源汇总，尚未生成新来源版本的历史季度继续读取原有效汇总。
- 用户确认历史期保留，不对 47 个已有季度批量回填；已实现历史旧汇总兜底，新来源汇总仅替换实际新采集/修订季度。
- 日常库回填前只读复核：历史 `sw_industry_cap_quarters` 为 18,359 行，新来源汇总为 0 行；实施期间未写入日常业务表。
- 锁释放后启动后台 PID 5136，8765 首页、`/api/industry/status` 和 `/api/industry` 均返回 HTTP 200；行业 API 返回 43 个现有历史汇总点。
- 保留 8765 后台运行，启动日志在 `data/verification/logs/industry-backend-20261014.log` 与 `industry-backend-20261014.err.log`。
- 隔离验证最终 32 项通过；临时测试库已清理。
