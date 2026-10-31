# 后端代码导航

从仓库根目录执行 `python app.py --port 8765 --open-browser`。主包为 `stock_analysis`，不再提供旧 `quarterly_dashboard` 导入路径。

| 目录／文件 | 职责 |
|---|---|
| `paths.py` | 稳定的项目、数据与前端根路径 |
| `runtime.py` | HTTP 服务、唯一调度器、退出与恢复重载生命周期 |
| `application.py` | 唯一数据服务、行业/AI/维护实例组装，以及初始化和恢复清理 |
| `http/` | server 保留协议/鉴权/门禁，routes 按功能分发；rendering 负责侧栏、页面模板与安全插值 |
| `infrastructure/` | SQLite 连接、事务、版本检查及网络基础；SQL 历史在 `migrations/` |
| `collection/` | 公共运行控制、来源归档、预算、锁、限速、调度及已注册任务适配；publication_counts提供不含IO的发布计数，collection_progress只读汇总公共批次 |
| `maintenance/` | 统计、备份恢复、空间整理和清理 |
| `domains/stocks/` | 股票库、行情、公告、分红、筹码；data_service 负责个股加载和数据组装，source_parsers 负责离线解析，sync_support 提供共享同步状态与策略 |
| `domains/stocks/collection_operations.py` | 个股首次/手动/分组执行编排与轻量运行状态；复用事实服务及公共来源，不复制业务缓存 |
| `domains/stocks/group_execution.py` | 分组业务依赖及有界工作线程，复用唯一operation和取消生命周期 |
| `domains/stocks/group_requests.py` | 独立来源通道、最多20股请求合并及全页校验/内存分流，同源仍走公共collector |
| `domains/stocks/group_batches.py` | 真实共享分段合并、逐股同事务计数和批次终态，不复制source |
| `domains/stocks/collection_schedules.py` | 分组规则/公共槽位适配、固定范围及管理页摘要；由唯一公共Scheduler调用 |
| `collection/collection_schedule.py` | 纯北京时间日历计算，包含日检查日期与财报季截止日前后窗口；不请求或读写数据库 |
| `collection/schedules.py` | 各模块多日程身份、合并配置及软删除；执行槽位仍由唯一Scheduler管理 |
| `collection/source_archive.py` / `source_archive_read.py` | 公共响应按月分卷、校验、清理与单文件恢复；透明读取和旧快照清单回退 |
| `collection/source_archive_service.py` | 本地归档手动操作及月度计划，复用后台生命周期、计划槽位和维护门禁 |
| `collection/schedule_overview.py` | 按独立规则只读关联最近5次计划运行，复用公共计数；日程启停校验及开关更新，不执行采集 |
| `collection/schema_upgrade.py` | v44控制元数据事务迁移、历史诊断归属及旧表退役，不联网 |
| `domains/stocks/collection_plan.py` | 个股真实来源分段规划与失败恢复选择，不创建独立item层 |
| `collection/execution_context.py` | 组合采集执行中的父任务元数据与取消回调，作用域退出自动恢复 |
| `domains/financials/` | 财务周期、报表、指标与主营构成；payloads 组装财务视图数据 |
| `domains/valuation/` | 估值、DCF、股息预测；纯计算在 calculations，视图数据组装在 payloads |
| `domains/indices/` | 指数定义、采集、查询、行情与编码 |
| `domains/industries/` | 通用行业分类、成员、财务／市值汇总与比较；company_preview.py 只读组装单公司快览 |
| `domains/financials/company_preview_metrics.py` | 行业公司快览纯周期、同比及派生计算；不引入网络或存储 |
| `domains/financials/company_preview_dividends.py` | 快览上年分红方案纯解析、保守年度合并及季末股息率估算，不取数、不写库 |
| `domains/market/` | 全市场财务、交易快照、拥挤度、国债及市场数据工具；market_financial_missing.py负责按公司／接口缺行的手动补缺，不修改既有行 |
| `domains/notes/` | 投资笔记与数据快照 |
| `domains/research/` | 研究报告、Checklist、经营判断、模型适配及行业专题 |

行业专题放在 `domains/research/sectors/<行业>/`，航运已归入 `shipping/`。领域专属采集、计算和存储在主题内组织，跨主题的预算、归档和请求锁复用公共采集层。

纯计算函数不主动取数或写库。已有 `valuation.py` 与 `update_service.py` 保留原函数导出供现有调用者使用；新增调用优先直接引用 `calculations.py` 或 `sync_support.py`。

`sources.py` 保留请求、分页和跨响应复权验证；纯解析实现位于 `source_parsers.py`，原解析导出仍兼容。数据服务显式接收 `infrastructure/app_settings.py` 的路径配置，领域模块不依赖 HTTP。

SQL、提示词、JS 解码器、字段定义、证书和 OCR 脚本为业务资产，位置跟随所属模块；不能仅按 Python 文件迁移。项目数据、来源响应、PDF、笔记和凭据仍在原配置位置。生成缓存不提交。

迁移阶段、完整文件映射和已知测试基线见 [计划](../docs/plans/20261025_代码目录结构整理/PLAN.md) 与 [验收](../docs/plans/20261025_代码目录结构整理/ACCEPTANCE.md)。

HTTP 与来源职责进一步拆分见 [本轮映射](../docs/plans/20261026_HTTP与来源职责拆分/MIGRATION_MAP.md) 与 [验收](../docs/plans/20261026_HTTP与来源职责拆分/ACCEPTANCE.md)。
