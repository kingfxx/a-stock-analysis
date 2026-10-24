# 任务目录（实施核对）

|task_key|入口/参数|互斥与运行依据|停止/恢复|来源及覆盖|
|---|---|---|---|---|
|market_financial|IndustryService.start_refresh financial_period / target 报告期、recheck|sw_update_runs，全行业任务串行，启动同步占位|无可靠用户取消；相同报告期重试，公共已完成分段/归档复用|eastmoney 日常双接口授权；四表公共batch，执行完成不代表公司字段齐全|
|trade_snapshot|IndustryService.start_refresh cap_quarter / target YYYYQn、recheck|同上，和财务互斥|不新增取消承诺；原范围重试|eastmoney snapshot策略；collection_batches + 固定季末名单，缺失如实保留|
|indices|index_collection.start_operation / incremental、full、review、audit、backfill、providers、start/end|index_collection_operations，和拥挤度/国债串行|cancel_requested；原参数续跑，公共分段幂等|csindex/legu/sws各自已有授权，公共来源预算与归档|
|market_congestion|market_congestion.start / incremental、review、full|同上|同上；首次full初始化需明确确认|legu；初始化归档，后续HTML metadata_only；保留原每日开关和时间|
|sovereign_yields|index_collection.start_operation datasets=[bond_zh_us_rate] / incremental|同上|同上|eastmoney东方财富，公共保守策略；管理页为独立增量，原指数页只读，旧联合历史保留；独立历史不混入联合任务整体状态|

业务页面已移除可执行批量入口，仅保留只读数据/进度及导航；统一采集管理使用上述真实服务。未改造CLI历史入口不自动纳入定时，不批量改写旧历史。个股到期检查、分类独立工具、BigQuant价格/历史合成、正式财报与模型不注册为可运行任务；航运未实现，不注册占位任务。

任务列表只读最近20条领域运行记录、其公共batch与来源摘要，不读取维护storage、不扫描事实表。覆盖仅展示这些batch的原判定与范围，并明确不是全历史覆盖。历史证据通过详情API引用，不复制来源响应。
