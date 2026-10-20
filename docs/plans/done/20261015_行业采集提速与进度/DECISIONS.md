# 决策记录

- Sol负责规划及审查部署；Luna负责实现测试，读写仅限本计划状态，不更新其他计划。
- 新参数仅应用于本轮全市场财务/季度交易快照；财务并发2、快照并发1为不同明确策略，等待2+0—3随机、每20次额外15秒。是否扩展到共用同一采集函数的手动历史工具须在文档精确说明；不自行启动这些工具。
- 公共API采用 `RequestCollector.policy_for('eastmoney', industry_profile='financial'|'snapshot')`，保留真实来源标识及既有默认、`stock_fast`、指数和CNINFO策略。日常全市场财务调用 `collect_public(..., parallel_interfaces=True)`；默认调用不并发，以免历史财务命令隐式扩展。复用 `collect_day` 的历史快照命令不自动启动，但显式执行时使用相同单并发快照等待策略。
- 财务并行采用最多2个工作线程、每接口独立Session和顺序分页；所有接口都完成后才能返回可发布数据。任一接口失败时，该operation剩余running批次统一关闭为failed，已完成接口不发布事实。来源拒绝统计来自各worker实际预留的请求，并在退出时汇总；worker及日常入口Session均关闭。
- 同源OS槽位形成一次两个在途请求的并行波次；波次中一个请求结束不能放行第三个请求绕过其完成间隔。所有策略继续共享 `collection_provider_state`，个股快速请求只豁免等待、不豁免占槽和预算/冷却。
- `sw_update_runs.result_json.collection_progress`为活跃采集进度；以事务内读改写方式合并，不覆盖`aggregation_results`。每个接口计数快照在同一进度互斥锁中写入，避免两个worker进度倒序覆盖。页数未知显示统计中；验证、统一发布、汇总分阶段标记。
- 原有个股快速策略保持，不承诺完全无影响：共享槽位可能使个股等待正在执行的批量请求，共享冷却会阻止同源两类任务；批量侧计入个股已发送记录。实现不引入独立来源预算规避共享保护。
- UI延续指数折叠进度风格；业务统计使用行业sw_update_runs而非index_collection_operations。下载与发布/汇总分阶段，业务成功必须对应保存完成。
- 当前工作区干净，上一轮实现已在HEAD；不提交、不推送，不改无关DCF或个股业务取数逻辑。
