# 后端代码导航

从仓库根目录执行 `python app.py --port 8765 --open-browser`。主包为 `stock_analysis`，不再提供旧 `quarterly_dashboard` 导入路径。

| 目录／文件 | 职责 |
|---|---|
| `paths.py` | 稳定的项目、数据与前端根路径 |
| `runtime.py` | HTTP 服务、唯一调度器、退出与恢复重载生命周期 |
| `http/` | HTTP 请求与响应、模板及静态资源映射；`rendering.py` 负责公共侧栏 |
| `infrastructure/` | SQLite 连接、事务、版本检查及网络基础；SQL 历史在 `migrations/` |
| `collection/` | 公共来源归档、预算、锁、限速、调度及已注册任务适配 |
| `maintenance/` | 统计、备份恢复、空间整理和清理 |
| `domains/stocks/` | 股票库、行情、公告、分红、筹码；`sync_support.py` 提供共享同步状态与策略 |
| `domains/financials/` | 财务周期、报表、指标与主营构成 |
| `domains/valuation/` | 估值、DCF、股息预测；纯计算在 `calculations.py` |
| `domains/indices/` | 指数定义、采集、查询、行情与编码 |
| `domains/industries/` | 通用行业分类、成员、财务／市值汇总与比较 |
| `domains/market/` | 全市场财务、交易快照、拥挤度、国债及市场数据工具 |
| `domains/notes/` | 投资笔记与数据快照 |
| `domains/research/` | 研究报告、Checklist、经营判断、模型适配及行业专题 |

行业专题放在 `domains/research/sectors/<行业>/`，航运已归入 `shipping/`。领域专属采集、计算和存储在主题内组织，跨主题的预算、归档和请求锁复用公共采集层。

纯计算函数不主动取数或写库。已有 `valuation.py` 与 `update_service.py` 保留原函数导出供现有调用者使用；新增调用优先直接引用 `calculations.py` 或 `sync_support.py`。

SQL、提示词、JS 解码器、字段定义、证书和 OCR 脚本为业务资产，位置跟随所属模块；不能仅按 Python 文件迁移。项目数据、来源响应、PDF、笔记和凭据仍在原配置位置。生成缓存不提交。

迁移阶段、完整文件映射和已知测试基线见 [计划](../docs/plans/20261025_代码目录结构整理/PLAN.md) 与 [验收](../docs/plans/20261025_代码目录结构整理/ACCEPTANCE.md)。
