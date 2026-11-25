# HTTP 与来源职责拆分验收

日期：2026-10-26。状态：本计划已完成，单代理执行。

## 交付结果

- server 从 1767 行缩为协议/鉴权/恢复门禁与启动入口；原领域路由归 http/routes 的 8 个具体功能组，公共 JSON 命令和顺序分发独立。
- StockDataService 显式接收 AppSettings，承担个股数据加载、缓存兼容、共享价格编排及页面初始数据。应用组装/初始化/恢复清理归 application；财务和估值 payload、HTML 渲染分别独立。
- sources 的 15 个原纯函数移入 source_parsers，另提取 3 个内存响应完整性校验入口。7 个请求函数、分页、跨响应复权核验和恢复回调保留在 sources；已有解析导出兼容，生产纯解析导入改指实际归属。
- 没有新增表/SQL 迁移、来源、采集入口、模型调用或前端功能。storage、前端内嵌脚本和 update_service 深度拆分不在本计划内。

## 回归与静态验证

| 检查 | 结果 | 本地证据（data/verification/reports/） |
|---|---|---|
| 当前基线 fba64bf 完整回归 | 1646 passed；106 文件，无失败/跳过/超时 | http-source-baseline/SUMMARY.json |
| HTTP 阶段重点回归 | 130 passed | http-source-http.xml |
| 来源阶段重点回归 | 149 passed | http-source-sources.xml |
| 最终完整回归 | **1652 passed；107 文件，无失败/跳过/超时** | http-source-final/SUMMARY.json 及逐文件 XML/日志 |
| 函数/路由处理体静态对照 | 101 项一致，35 组 GET 条件顺序一致 | http-source-extraction.json |
| 禁止网络/SQLite 连接导入 | 146 模块成功 | http-source-imports.json |
| 静态 HTTP | 39 项字节/MIME、4 拒绝路径、2 页面通过，禁止数据库连接 | http-source-assets.json |
| 非 Python 业务资产 | 128 项与 Git 基线内容一致，比较允许 checkout 换行差异；另外记录当前文件 SHA256，不冒充迁移前原始字节快照 | http-source-asset-hashes.json |
| 静态编译、app.py --help、git diff --check | 通过 | 执行记录与当前 diff |

完整回归复用 `run_structure_regression.py`：按文件独立运行，使用明确临时库、外网禁止插件、最多两个测试进程和逐文件超时。HTTP/浏览器测试服务为临时实例，均结束；没有另开持续后台。fixture 同时隔离数据库、fundamentals、valuation 和 chips。

新增 6 项边界验证：纯解析独立导入/执行、配置与实例状态隔离、JSON 脚本转义、恢复状态在排空期仍可读、未知方法及请求校验契约、功能路由方法不能重名覆盖。

首轮失败涉及迁移后测试补丁目标、旧 SimpleNamespace 假 Handler 及新增边界测试整理；已修正并在最终完整回归全部通过。提取时发现的 JSON 转义和路由重名已修正并加入验证。指数即时读取断言曾出现一次空列表，未改前端或放宽断言，单独及最终完整复测均通过。

AST 比较只用于明确的原函数/处理体和顺序，不能替代 HTTP、分页及恢复测试。三个来源新校验接口和维护服务注册查找依赖行为回归，不宣称所有新文件与原版整体 AST 相同。

## 宿主机 8765 验收

- 原 PID42268 身份已核实：Python311/python.exe -u app.py --port 8765；重启前采集空闲。
- 新后台 **PID46060**，继续使用原 data/stock_analysis.sqlite3 并保持运行。
- `/`、`/indices`、`/?code=601919&tab=shipping-forecast` 均 200；分别为 3236489、13120、6075 字节，与重启前一致。
- schema 为 42；schema_migrations、collection_schedules、collection_schedule_rules、collection_rate_limits、collection_provider_state 与重启前逐项一致。没有活动采集，没有手动触发来源/回填/模型。
- 证据：http-source-live-predeploy.json、http-source-live-final.json、http-source-live-comparison.json。
- 当前日志：data/verification/logs/structure-server-http-source-final.out.log / .err.log。初始化与监听正常；初始短超时就绪探测导致已断开连接的 ConnectionAbortedError 记录，随后正常页面请求通过。没有将此传输日志描述为业务启动失败，也没有借本次重构更改静态回应的异常契约。

## 复核与维护入口

- 逐项归属：[MIGRATION_MAP.md](MIGRATION_MAP.md)、migration.json、source-migration.json。
- 可复用验证工具：data/verification/tools/verify_http_source_extraction.py、verify_app_imports.py、verify_structure_assets.py（本轮增加可选 --output，保留原默认行为）、run_structure_regression.py、structure_test_guard.py。
- 临时库、测试浏览器目录、一次性提取脚本和过期失败 XML 已清理。日常数据库、正式备份、正式财报、原始来源及当前后台日志保留。
- 用户验收后授权统一提交本轮全部修改，提交信息为 `refactor: separate HTTP routes data assembly and source parsing`；不推送，实施前基线为 fba64bf。回退仅限本任务源码及文档，不覆盖日常数据库或清空来源状态。
