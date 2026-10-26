# 实施后交接：统一采集管理

2026-10-27个股接入已完成：取消页面到期自动采集、保留手动/全量、首次新股初始化及分组定时；不增加业务缓存或响应原文。完整回归1666 passed，补充回归42 passed；原库已自动备份并升级v43，原8765当前PID43148保持运行。原日程/限速配置哈希、source/batch水位前后不变，新个股日程为0条。状态与验收见本目录PROGRESS/ACCEPTANCE末节；覆盖下文“个股尚未决定”。

**最新状态（2026-10-25）：用户已自行重启8765，PID34708，schema42。原库迁移及只读实页验收通过，原定时与配置原样保留，9个网络来源表单、模板、未保存刷新保留及390px窄屏无异常。未在原库保存自定义参数或启动额外采集，报告见PROGRESS末节。**


本计划已于2026-10-25单代理实施，相关回归与8765验收完成。后续缺陷／反馈继续沿用本目录，不创建重复计划。

先读根AGENTS、本目录PLAN/DECISIONS/PROGRESS/TASK_CATALOG/ACCEPTANCE及docs/collection-management.md。航运仍不在本任务范围。

- 核心实现quarterly_dashboard/collection_management.py，v40迁移040_collection_management.sql。
- 原业务入口和管理页共享领域运行／数据库互斥。原拥挤度设置保留，其余自动检查默认关闭；没有自动真实回填。
- 服务器只启动一个统一Scheduler；旧拥挤度daily适配调用原scheduled_tick，不再启动旧DailyScheduler。
- 管理列表SQL摘要化，完整结果点击懒读取；数据库预览始终独立懒加载，隐藏页事件只标脏。
- 行业用户停止暂不支持；内部cancel_requested用于安全停机。财务／快照自动检查为明确单报告期／季度，不自动滚动目标。
- 宿主机8765原库schema40，最终进程30504，日志data/verification/logs/collection_management_8765.stdout.log及stderr.log，后台保留供验收。
- 最终相关回归164通过；3项既有失败已在HEAD副本复现，详情见ACCEPTANCE。没有声称全仓库无失败。
- 已保护工作前的计划归档与航运实验未提交内容；Git提交/推送状态以仓库历史为准。

页面反馈已修正：两页共享collection-progress.js/css；按任务确认；国债来源eastmoney，独立历史不混联合失败。追加相关回归67通过，见PROGRESS/ACCEPTANCE末节。

最新反馈：取消重复置顶摘要，默认展开最新时间戳记录；公共分段折叠，提供方下拉多选。拥挤度已按用户授权统一最近到期补跑（次日早晨补前一晚），collection_schedule.py共用计算。最新相关84通过。用户已启用指数/国债21:00和拥挤度18:00，配置保留。

最新入口调整：菜单裁切修复；最近四期/手填，手动默认最新，定时已保存目标不变；原指数/拥挤度/行业采集按钮移除，仅只读及导航。根AGENTS约束批量入口统一，个股尚未决定保留现状，分析见INDIVIDUAL_REFRESH_OPTIONS.md。最新相关87项通过。

2026-10-27后续反馈已完成：同组多主题规则及编辑/删除已上线，旧持仓19:00规则保留。原8765当前PID42524，schema仍v43，无新增迁移或缓存；验收和日志以PROGRESS/ACCEPTANCE最后一节为准。
