# 进度

## 2026-10-16

- 已阅读项目根规则、采集规则、财务规则、个股采集说明及 SQLite／维护说明。
- 已核对 FundamentalService、stock_collection、update_service 的实际流程与24小时检查、180天审计、600秒失败抑制。
- 已完成 PLAN.md 与 DECISIONS.md：十年范围、实际报告期增量、独立水位、结构化版本与明细、metadata_only、完整分页及验收范围。
- 仅新增当前计划目录三份文档；未修改业务代码、数据库、既有运行文档或其他计划状态，未新增网络查询、原始响应归档或定时任务，未重启8765。
- 工作区原有 web/financial-statements.js、web/index.html、web/industry.css、web/industry.js 及其他计划目录变更保持原状。
- 后续工作：按 PLAN.md 实施迁移、解析、服务接入、隔离库测试与有限样本验收；当前未实施，不声称已可采集或展示。

## 协作实施启动

- 用户明确授权实施和协作模式；规划与决策已更新，执行代理将按当前目录三个文件开发、测试并更新本文件，主代理随后审查代码及结果。
- 工作区既有前端及其他计划变更不属于本任务，不覆盖或回滚。

## 执行进展

- 已新增34号迁移及两张业务表，复用sync和公共collection；明细具有source/batch复合外键及父版本身份核验。
- 已实现Decimal解析、十年窗口、500条分页及首页复核、整段发布、幂等与真实版本修订、缺项／字段撤回保护、24h／180d／600s独立状态。
- 已接入financial普通／refresh／full及loading到期判定，加入独立异常保护；新增只读GET /api/business-segments，未修改现有前端。
- 已登记维护用途和父版本实际取得时间，纳入未关注股票清理；多版本倒序删除及整套引用保护已补齐。
- 隔离模拟测试 `python -m pytest tests/test_business_segments.py -q --basetemp=data/verification/business_segments_test_tmp4`：22通过，包含版本A→B→A、精度、分页变化／截断、空值及缺项、空结果、缓存、审计、独立失败、清理与只读API。
- 扩展回归在沙箱运行68通过、4失败，失败均为既有HTTP测试触发WinError10013套接字限制；将提权使用隔离目录复测。未运行真实采集、未写日常库、未启停8765。
- 正在补充三份运行／存储文档与剩余冷却、事务、维护统计检查；最终交主代理审查和真实验收。

### 扩展回归排查

- 首次提权9文件回归198通过、4失败；完整nodeid：
  - `tests/test_industries.py::test_versions_same_quarter_and_failed_import_preserve_old`：`market_series[0].trade_date`得到2026-09-29，旧测试期望2026-09-30。该测试不走个股financial／MAINOP链。
  - `tests/test_industries.py::test_financial_task_one_period_reuses_local_and_never_fetches_caps`：公共原文归档的临时UUID路径FileNotFoundError；basetemp路径过长疑似Windows260字符限制，短路径复测中。
  - `tests/test_industries.py::test_quarter_cap_default_skip_and_revision_only_changed_company`：同上公共原文路径FileNotFoundError，短路径复测中。
  - `tests/test_industries.py::test_industry_api_requires_explicit_task_stock_routes_stay_separate`：测试mock整个load_chart_data仅返回简化数据，原价格bundle路由访问data['valuation']发生KeyError→RemoteDisconnected。与新MAINOP服务调用无关。
- 新增数据集服务测试扩展至24项，补共享429冷却、原地修改拒绝与来源完整性；同时给既有p4／股票简称集成测试增加MAINOP stub，防止测试新增真实网络调用。
- 最终11文件回归采用较短 `--basetemp=data/verification/bsr`，仍仅隔离库和模拟来源；代码未改无关行业行为。

- 短路径11文件回归208通过、7失败：长路径导致的financial_task失败消失；quarter_cap原断言为total_cap应320但得到None。主代理将Git HEAD代码只读导出至隔离目录验证，行业3项（日期、total_cap、price_bundle valuation）均复现；新增发现 `tests/test_stock_names.py::test_name_failure_keeps_facts_and_retries_on_next_update` 的4参数（None、空字符串、三个空格、offline）同样在Git HEAD复现：旧测试立即重试期望名称成功，与现有600秒失败抑制冲突。本轮不改变这些既有行为。
- 最终相关回归排除上述7项已证实基线失败（用-k排除4个测试函数），并增加首次自动／自动审计真实trigger及失败600秒loading测试；最终结果待写入。

## 执行交接

- 最终相关回归命令：`python -m pytest tests/test_business_segments.py tests/test_announcement_server_integration.py tests/test_async_loading.py tests/test_stock_cleanup.py tests/test_stock_collection.py tests/test_maintenance.py tests/test_storage.py tests/test_fundamental_service.py tests/test_p4_server_integration.py tests/test_stock_names.py tests/test_industries.py -q -k 'not test_name_failure_keeps_facts_and_retries_on_next_update and not test_versions_same_quarter_and_failed_import_preserve_old and not test_quarter_cap_default_skip_and_revision_only_changed_company and not test_industry_api_requires_explicit_task_stock_routes_stay_separate' --basetemp=data/verification/bsf`：**209 passed, 7 deselected in 32.98s**。使用提权仅允许现有HTTP本机测试，业务来源全部模拟。
- 最后增加精确来源股票请求身份触发器、业务版本／明细不可原地修改约束、Decimal负零规范和整数字符串排序解析后，再跑 `python -m pytest tests/test_business_segments.py -q --basetemp=data/verification/bsn`：**25 passed in 6.14s**；`git diff --check`通过（仅既有CRLF提示）。
- 维护页已验证新表发现、用途／个股分类、空表未记录、明细父表取得时间、真实来源时间和外键；多修订清理与事务回滚通过。首个自动建档及180天自动审计均记录automatic，显式full才manual_full；新增数据集失败600秒内不单独让financial loading持续触发。
- 本执行代理创建的9个临时测试目录、库及其临时备份已核对绝对路径并清理；不清理主代理验证目录、正式库、正式备份、财报或现有前端变更。
- 接入入口：个股financial普通／refresh／full自动独立检查；本地只读 `GET /api/business-segments?code=605499`（可选start/end）；Python `BusinessSegmentService(Database(path)).update(code, full=True)`可单独执行主营构成有限验收，无需连带财报／行情来源。
- 未写日常库、未真实请求、未提交Git、未启停8765；主代理继续执行代码审查结论、日常迁移、两样本十年有限真实验收、报告与宿主机8765启动。执行层开发测试完成，不宣称来源逐字段覆盖完整或已提供图表。

## Sol审查与正式验收完成

- 主代理审查了迁移、采集／版本服务、financial及loading接入、来源校验、维护和股票清理。修正独立异常隔离、多修订倒序清理、整套引用保护、自动触发类型等问题后通过审查；未改无关行业逻辑，未覆盖用户既有前端变更。
- 最终主代理复核：`python -m pytest tests/test_business_segments.py tests/test_fundamental_service.py tests/test_stock_collection.py tests/test_p4_server_integration.py tests/test_stock_cleanup.py tests/test_maintenance.py -q --basetemp data/verification/bsfinal`：**104 passed in 20.82s**。语法检查和git diff --check通过。
- 7项排除的旧失败已分别在只读导出的Git HEAD隔离副本复现，非凭推测排除：行业3项（季度交易日、季度市值、价格bundle模拟响应缺valuation），简称4参数（立即重试与现有600秒失败抑制冲突）。不声称全套测试零失败。
- 宿主8765启动前无监听；已启动本仓库 `python -u app.py --port 8765`，PID25548，原库结构33→34。自动迁移前正式备份：`data/backups/pre-migration-20261016T064217876615Z.sqlite3`。启动日志无错误，中断任务恢复数均0，后台保持运行。
- 真实验收仅调用BusinessSegmentService对601919、605499各一次full，查询窗口2016-10-16至2026-10-16，共2个HTTP请求；未触发全市场、其他个股数据集或模型。
- 601919：20期、244条，2016-12-31至2026-06-30；605499：19期、215条，2018-12-31至2026-06-30。实际取得时间、请求／解析完成及run/batch/source关联已核对；两请求原文路径和双哈希均null。来源缺成本／毛利字段原样留空，不宣称逐字段覆盖完整，batch coverage_status=unknown。
- 两股只读API均HTTP200，普通update缓存复用及页面读取合计新增来源记录0；首页HTTP200，维护接口发现版本39行／明细459行、分类个股数据、大小与实际取得时间正确。明细时间通过父版本取得时间关联。
- 原数据库check：schema_version=34、integrity=ok、foreign_key_check无违规。验收统计：`data/verification/reports/business-segments-acceptance-20261016.json`，不含原始响应。
- 当前日志：`data/verification/logs/business-segments-8765-20261016.stdout.log`、`.stderr.log`。主代理基线副本及回归临时库／临时备份均已核对路径清理；正式迁移备份和验收资料保留。
- 本计划完成；当前交付为采集、结构化版本存储、个股财务更新接入和只读接口，没有新增图表或自动跨期分类归并，未提交Git。
