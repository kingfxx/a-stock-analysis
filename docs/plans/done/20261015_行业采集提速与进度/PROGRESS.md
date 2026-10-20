# 进度

- 已完成公共限速profile：`financial` 并发上限2，`snapshot` 并发1；二者请求完成后2—5秒、滚动60秒30次、每20次额外15秒。与指数10秒休息、CNINFO及个股`stock_fast`行为分离，预算／拒绝冷却／OS槽位仍共享。
- 日常四表入口显式并行，使用最多两个worker及独立Session；接口内分页顺序执行，统一完整校验后返回待发布数据，调用者仍在原单一事务发布四表。任一并行接口失败后，同operation所有未发布批次标为failed，实际请求/重试汇总，关闭worker及日常入口Session。`collect_public`默认仍串行，因此手动财务历史回填没有被自动切换策略。
- `collect_day`请求显式使用snapshot profile，仍串行分页；包括显式执行的历史快照命令，不会自动启动。快照进度覆盖分页、保存及行业汇总状态。
- `sw_update_runs.result_json.collection_progress`以事务内合并写入，保留`aggregation_results`；进度按钮分别标记“查看财务进度”和“查看快照进度”，默认收起并通过`aria-expanded`/`aria-controls`控制，轮询仅更新文本。
- 修复陈旧的v32迁移测试fixture：明确从v31结构开始并只应用到v32；基线原先因HEAD已有v33迁移而错误期待数据库停在v32。
- 已通过隔离回归48项：`test_collection_provider_pacing.py`、`test_market_financial.py`、`test_market_trade_snapshot.py`、`test_industry_collection_review.py`、`test_industry_parallel_collection.py`全部通过；新增 `test_industry_progress.py` 覆盖运行期进度、`aggregation_results`保留及旧记录未知状态。`node tests/industry_ui.cjs`验证两个进度按钮默认收起、ARIA/hidden状态及点击展开/收起。
- Sol运行的15文件组合回归首轮为174通过、5失败，失败均为旧季度快照测试替身签名未接受新增`progress_state`关键字；已更新`test_industry_memberships.py`和`test_industry_cap_summary.py`三个替身，随后两文件35项全通过。完整组合回归由Sol继续复验。
- Sol按计划运行有限隔离验收样本：财报期`2026-06-30`的income及balance各1个HTTP请求、各校验500行，观测并发2；来源哈希只读核验通过，事实表写入0。报告见`data/verification/reports/industry-parallel-live-20261015.json`。未执行日常全量采集。
- HEAD基线对照已完成：`test_versions_same_quarter_and_failed_import_preserve_old`（日期期望与fixture不一致）及`test_industry_api_requires_explicit_task_stock_routes_stay_separate`（旧server测试替身缺少`valuation`）在归档HEAD中复现失败；`test_quarter_cap_default_skip_and_revision_only_changed_company`也在短路径HEAD归档中复现`total_cap is None`。三项均为既有测试/fixture问题，本轮未改业务代码处理。基线副本及临时库已清理。
- 最终15文件组合回归179项全部通过，Node折叠交互测试通过；Sol完成8765宿主重启（PID 30760）与页面验收，财务/快照按钮均默认收起且可展开，状态接口HTTP 200。截图：`data/verification/reports/industry-progress-20261015.jpg`。仅执行前述两次有限取样，未启动全量更新或提交；本轮临时测试库已清理。Sol审查完成。
