# 职责迁移与 HTTP 契约映射

基线：`fba64bf`。逐项机器清单见 [migration.json](migration.json) 和 [source-migration.json](source-migration.json)，其中行号为基线源码定位，当前入口以本表和实际命名为准。

## 代码归属

| 原位置 | 新位置与职责 |
|---|---|
| server 的 ROOT/CACHE/VALUATION_CACHE/CHIP_CACHE/DATABASE_PATH | infrastructure/app_settings.py：AppSettings，由 application 构造后传入 StockDataService |
| server 的 industry_service/ai_service/business_service、初始化/工作检查/恢复/清理回调 | application.py：应用组装与生命周期；服务锁复用 data 的唯一锁 |
| server 的 services/p4_services、服务/数据锁字典 | StockDataService 实例；恢复时 application 清理同一份字典 |
| server 的 load_stock/load_valuation/load_dividends、缺值检查及旧 JSON 缓存辅助 | domains/stocks/data_service.py：原算法及旧缓存兼容，不再依赖 Handler |
| server 的 load_chips/shared_projection/price_bundle/load_chart_data、保存价格适配和加载状态 | domains/stocks/data_service.py：共享版本、刷新编排与失败保留 |
| render_page 中业务准备 | StockDataService.page_payload：返回代码、初始数据与错误文本 |
| render_page 中 HTML 插值、侧栏和展示版本 | http/rendering.py：模板、JSON 转义及 HTML 转义 |
| server 的 _financial_payload | domains/financials/payloads.py |
| server 的 _valuation_payload/_p4_valuation_payload | domains/valuation/payloads.py |
| server 的缓存版本/字段常量与 needs_dividends 纯判断 | domains/stocks/data_contracts.py |
| server 的公告查询和分类/来源 URL 组装 | StockDataService.read_announcements |
| Handler 的领域处理方法与路径分支 | http/routes 各具体模块，见下表 |
| Handler 的 Host/Origin/会话鉴权、JSON 回应、日志脱敏、请求门禁及 GET/POST/PUT 适配 | http/server.py；serve 仍为 app.py 启动入口 |
| sources 的数值/日期/代码/名称、财报/现金流、月日 K/OHLC 解析及字段常量 | domains/stocks/source_parsers.py；15 个原函数逐一保持函数体 |
| sources 内嵌的单页完整性及完整历史计数校验 | source_parsers 的 parse_financial_report_page/validate_financial_history/parse_cash_flow_history |
| sources 的 7 个 fetch 函数、URL、Session、分页和跨响应复权验证 | 仍在 sources.py；原解析名称继续兼容导出 |

## 路由分组

所有现有路径保留，分发顺序与基线一致。35 组 GET 条件覆盖精确路径、集合路径与前缀路径，并非只有 35 个 URL。

| 模块 | 方法与入口 | 契约要点 |
|---|---|---|
| shipping | GET `/api/shipping-trend`、`/api/shipping-forecast`；POST forecast 的 save/prepare-report/delete | 仅 601919；保留 query 参数、Host、请求体限制和本地会话校验；未授权 POST 有界读完正文后返回 403；原 ValueError 400、运行错误 503 边界保持 |
| notes | GET `/api/notes` 及 `/api/notes/*`；POST `/api/notes/*`；PUT `/api/notes/document` | 路径、目录版本、冲突 409、内容/体积限制、Host 和写入鉴权保持；resource MIME、nosniff、sandbox CSP 与 no-store 保持 |
| maintenance | GET collections/operation/evidence、backups、unfollowed、stock-cleanup、storage；POST collections 的 run/cancel/schedule/rate-limit、compact、stock-cleanup、restore 和 unfollowed | query、请求字段白名单、错误码、上传上限、恢复 202/关闭顺序保持；evidence 仍只读返回最多 200 条元数据；维护实例创建移至 application |
| research | GET/POST `/api/ai/*`、`/api/analysis`、`/api/analysis/*`、`/api/business-judgment*`；GET `/auth/callback`、`/api/research-reports`、`/research-reports/*` | 模型只由明确写操作触发；保留 Host/Origin/会话、请求体 8192、准备/取消/历史路径、错误处理及 OAuth 日志脱敏；财报文件白名单与 nosniff 保持 |
| indices | GET indices/congestion、catalog/status/chart/price/crowding/industry-crowding/equity-bond-spread、indices/source；POST collect/cancel/congestion/schedule | 原 query、来源查看、只读状态、采集命令字段及异步线程调用顺序保持；读写错误边界保持 |
| industries | GET `/api/industry`、`/api/industry/status`；POST `/api/industry/refresh` | 原 level/mode/period 等参数、业务参数错误 400、存储错误 503，以及显式更新语义保持 |
| stocks | GET business-segments、stock-announcements、stock-groups、institution-forecast、dividend-forecast、dcf/wacc、statements、financial/dividends/valuation/shareholders/financing/prices；POST stock-groups | 保留参数默认、取消关注限制、刷新/full/price_version、版本过期 409、部分接口 400/503 差异和估值压缩；无读取触发模型行为 |
| pages | GET `/`、`/indices`、既有脚本/CSS、`/vendor/notes/*`、`/samples/industry-summary.html`、Plotly | 静态映射/白名单、字节/MIME保持；query 处理、侧栏插值与航运 tab 保持；Plotly 缓存一天，其他原有 no-store/无缓存头差异保持 |
| server | GET restore/status 的排空期例外；统一 GET/POST 分发；PUT 笔记适配 | 排空期普通请求 503；restore/status 只读内存状态并核对 Host；未知路径 404、非笔记 PUT 405；不改变未实现方法的基类行为 |
| commands | 既有普通 JSON POST 公共校验及领域命令分发 | 可选 Origin 校验、JSON MIME、1—65536 字节限制、原错误文本及异常边界保持；与 AI/笔记/维护上传/航运专有校验分开 |

## 验证与兼容

- `verify_http_source_extraction.py --base fba64bf` 静态核对 101 项函数体/路由处理体及 35 组 GET 顺序。该比较只归一化显式依赖名称、实例方法和共享回应提取，不忽略计算或条件差异。
- 三个新来源校验入口和来源请求顺序由离线来源契约测试验证；维护实例查找迁移由 HTTP/恢复测试验证，未将它们冒充完整 AST 相同。
- HTTP server 的原业务函数导出不再保留：已知生产调用者只有 app.py 的 serve，测试及直接函数导入均迁移到真实归属。不添加全局代理、sys.modules 别名或双向导入。
- sources 的解析兼容导出保留；生产纯解析调用者直接导入 source_parsers，请求调用者继续导入 sources。无新增真实来源、预算或证据副本。
