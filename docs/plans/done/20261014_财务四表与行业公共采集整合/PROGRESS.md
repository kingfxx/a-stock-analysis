# 进度

## 2026-10-14 · Sol 分析与规划

- 已读项目通用规则、批量采集规则、财务与财报相关规则。
- 已查四表采集／历史入口、公共 collection.py、指数及交易快照接入示例、分类成员与导入流程、旧工具入口、迁移结构、维护统计、财务 View／来源／缓存依赖和相关测试文件位置。
- 已对原日常库执行只读表计数、报告期范围及公共数据集查询；schema=31，财务四表均覆盖 48 个有记录报告期。未将其认定为逐指标完整。
- 已编写 PLAN.md 和 DECISIONS.md，覆盖全部 14 张 sw_ 表、来源映射、兼容结构、恢复／覆盖、旧入口收口、实施阶段与验收。
- 尚未实施；未运行网络请求／采集、数据库初始化／迁移／备份、自动测试或服务重启。只新增本计划目录三个 Markdown 文件。

## 待授权后的交接

Luna 读取本目录三个文件，复核最新 schema／Git 状态／任务运行状况后按 P0—P5 执行；每阶段记录实际变更、验证与偏差。Sol 最终审查本目录及 Git diff。

旧来源元数据迁移、历史再回填、旧表清理与定时任务不属于自动执行项。

## 2026-10-14 · Luna 实施

- 用户明确授权按计划执行，覆盖上述“尚未授权实施”文字；范围仍受计划边界约束。
- 复核 Git：用户已有 `AGENTS.md` 修改及本计划目录未跟踪，均保留。只读复核原库 schema=31，`sw_update_runs` 无 running 任务；未初始化、备份或写入日常库。
- 原库财务表当前记录数：income 226,479、balance 219,142、cashflow 226,153、performance 225,630。文件 1,443,299,328 字节，SQLite page_count 352,368 × page_size 4,096 = 1,443,299,328 字节（约 1.34 GiB）；E 盘可用约 333.56 GB。
- P0 入口审计进一步确认 `RequestCollector.get` 将 403/429/反爬视为拒绝，但遗漏 401；已把 401 纳入同源立即停止与冷却逻辑，并为隔离测试增加 401 参数案例。
- P1 隔离实现：schema v32 机械重建四张财务事实表，按原列顺序复制历史值，旧来源列改可空并新增公共来源/批次组合外键及二选一约束；行业财务 View 暴露新旧两套显式引用。`sw_memberships` 增加公共上市日期来源对，维护页登记描述及真实采集时间路径。
- 新旧 reader 路由已实现：行业 reader 按独立引用列解析公共或旧来源；服务缓存 token 和报告期财务时间合并旧批次与公共批次。未按整数 ID 猜来源命名空间。
- P2 日常一期已接公共 collector：每报告期×财务表建立独立公共批次、逐页归档 source 和双哈希，经过范围/全字段/分页/证券唯一性检查后，四个批次事实在单事务发布。日常入口使用现有 `sw_update_runs` id；直接调用也取得任务行。来源与批次、operation、数据集、解析状态、接口及页码口径在发布前核对。
- 修复旧发布语义：无业务变化不改写事实来源；NULL 补成有效值新增可追溯事实版本；有值变 NULL 报疑似撤回并阻止发布。字段、来源和事实变更保持事务原子。
- P1/P2 隔离测试：旧 schema v31 迁移逐列逐值保真、索引/View/外键校验；公共财务模拟响应四表采集发布；公共引用 provenance；401拒绝冷却。最新财务/reader/collection 回归 `61 passed`（`tests/test_market_financial.py`、`tests/test_industry_financial_reader.py`、`tests/test_index_collection.py`）；日常单期公共入口约束测试 `1 passed`。临时测试库均在 `data/verification/` 下。
- P2 历史入口现已改用相同公共 `collect_public` / 原子 `publish`，按 `sw_update_runs:<id>` 关联 operation；仅四个数据集均为同 operation、范围及覆盖定义匹配的完整批次才可跳过。旧期已完整采集时显式使用 `--recheck` 才会走新公共路径；这不是旧来源迁移。空结果不作为可跳过完成，失败会终止本期仍运行的公共批次。历史入口只做模拟测试，未执行真实回填。
- 审查修正：公共财务批次 `status=complete` 只代表请求段通过分页校验并成功发布；有行数据时 `coverage_status=unknown`，不再把分页成功误报为全市场/全字段覆盖完整。结果记录 request/source/verified/published counts，以及逐字段非空/空值计数。历史跳过检查依赖四段 `status=complete`、operation、报告期 scope、parser/universe/definition 与 ALL 查询口径，不依赖 coverage_status='complete'，但拒绝 `empty` 覆盖。新增隔离断言验证 unknown 状态仍可由严格四段判据识别为已执行、empty 不会被跳过，且统计写入 result_json。
- 修正后财务、reader、collection 回归再次为 `61 passed`；增加的历史 skip/empty 判据测试 `1 passed`。
- 隔离容量演练报告：`data/verification/reports/20261014_financial_v32_capacity.json`。按原四表记录数建立合成稀疏行（仅代表行数，不是原库逐列内容），v32 迁移耗时 4.44 秒；主库文件从 190,017,536 增至 225,320,960 字节，迁移后目录（含迁移自动备份）415,338,496 字节，外键错误 0，记录数逐表一致。报告显示可用空间减少约 225 MB。临时合成库已清理。该演练验证迁移机械路径和行数，不代表原始库复制后实际文件大小、完整值保真或峰值空间；v31 原始逐列逐值保真由小型隔离迁移测试验证。
- 当前完成：P0 拒绝处理修复；P1 schema 双引用兼容、迁移与 reader/cache/maintenance 路径；P2 日常一期财务及历史回填公共 collector 代码和模拟验证；P3 分类名单/成员公共采集器及每日检查发布；P4 计划点名的旧在线入口停用或导向新公共入口，离线解析和本地证据复用保留。P2 尚无公共分页来源的跨重启缓存复用；恢复同范围 operation 会重新请求分页，旧来源不自动迁移。
- 明确未完成：P5 完整隔离副本迁移演练、完整页面／维护视图验收和部署。稀疏合成迁移及小型逐值迁移测试不能代替原库完整隔离副本验证。`test_industry_memberships.py` 有 22 项通过、耗时较长的 4,000 成员联合市值用例 1 项未验收；核心财务／来源回归 70 项通过；P3 改动相关 `test_industries.py` 两项通过。未执行真实网络采集、日常库迁移／备份／改写、历史回填、定时任务或 8765 重启。

## 2026-10-14 · Luna P4 旧在线入口收口

- 按计划列出的旧入口重新扫描调用链。分类主入口 `industry_memberships.fetch_directory` 与 `ensure_daily_memberships` 使用公共 `RequestCollector`；日常/历史财务入口使用 `market_financial.collect_public`。旧 `industry_sources.download`（含 foundation-only 和混合试点）、`market_financial.collect` 与 `collect_dataset`、`industry_bulk.collect`、旧财务扩展／业绩补采、`industry_updates.fetch_financial_period`、旧季度交易日／市值请求，以及 `industry_cap_history` 缺本地证据时的百度请求，现在停止联网并提示新公共入口。离线解析函数（含财务行、报价、申万 XLS、交易所名单、百度历史口径解析）、只读历史和本地证据复用保留。
- 旧分类 CLI 错误改为 argparse 用法提示并指向 `industry_memberships`；旧财务扩展、业绩和历史市值 CLI 同样显示停用原因。文档更新 `market-financial-statements.md`、`sw-industry-pilot.md`、`sw-listing-date-research.md`，说明当前入口、旧历史保留和本地证据限制。
- 增加 `tests/test_collection_entrypoints.py`，覆盖旧 collector 停用消息、离线财务解析保留、旧分类 CLI 指引，以及历史市值缺本地证据时不联网。
- 首次使用系统 pytest 临时目录时 Windows 原子归档写入报路径错误，改用仓库内隔离临时目录。最终 `tests/test_collection_entrypoints.py`、`test_market_financial.py`、`test_industry_financial_reader.py`、`test_index_collection.py` 合计 `70 passed`；`test_industry_memberships.py` 为 `22 passed, 1 deselected`；P4 改动涉及的两个 `test_industries.py` 用例 `2 passed`，财务有值→NULL 撤回保护修正用例 `1 passed`。`py_compile` 与 `git diff --check` 通过。宽筛选中另有旧断言／本机网络沙箱相关失败，且 4,000 成员慢用例未纳入最终验收。
- 当前 P4 未执行真实采集、日常数据库读写／初始化／备份、历史回填或后台重启。P5 尚未完成：未做完整克隆库迁移演练、页面／维护视图完整验收或 8765 部署重启；完整回归并未通过，不能表述为全套验收通过。

## 2026-10-14 · Sol P5 补充验收

- 使用 SQLite Backup API 从日常库只读生成 1.44 GB 隔离副本，在副本执行 v31→v32 初始化／迁移；逐行比较四张财务事实表所有迁移前字段，income 226,479、balance 219,142、cashflow 226,153、performance 225,630 行哈希完全一致。迁移后 `integrity_check=ok`、外键错误 0。
- 隔离副本 `MaintenanceService.read(refresh=True)` 成功：68 张表、2 个 View，未发现用途未登记表。详细结果见 `data/verification/reports/20261014_financial_v32_full_clone.json`。克隆库和迁移备份已清理；没有复制/改写源 PDF 或原始响应，因此这不是生产完整备份。
- 单独运行 4,000 成员跨服务／跨日检查用例：`1 passed`；成员归档关键选择 `7 passed`。核心财务／来源／旧入口组合及撤回保护用例 `71 passed`；成员其余快速用例此前 `22 passed, 1 deselected`。
- 维护统计的 HTTP 监听器测试受 Windows 本地套接字策略拒绝（WinError 10013），同次维护服务本身 `7 passed`；无法据此声称 API/页面实机验收通过。8765 未发现监听进程，未启动或重启后台。
- P5 的完整克隆迁移与数据库维护统计已验证；后台页面/API 实机验收、8765 部署检查仍未完成。P2 公共财务分页跨重启来源复用仍是已记录缺口。没有真实网络采集、日常库迁移／写入、正式备份或历史回填。
- 用户随后要求重启后台继续验收。确认 8765 无监听，但启动 `python app.py --port 8765` 被 `data/stock_analysis.sqlite3.lock` 的操作系统锁拒绝（WinError 13；`instance_lock` 报 Another application/maintenance instance is using this database）。进程枚举工具在当前环境不可用，不能确认锁持有者；按 `storage.py` 约定，不删除锁文件或绕过互斥。复核原库仍为 schema 31、无 running 任务、文件时间/大小未变；本次未发生迁移，启动日志保存在 `data/verification/logs/daily-8765-financial-v32-20261014.*.log`。需要先关闭持锁的应用/维护实例，再继续启动验收。
- 用户确认已关闭持锁实例后，复核 `instance_lock` 可取得、8765 无监听、原库 schema 31 且无 running 任务。随后以 `python -u app.py --port 8765` 启动，正常完成 schema 32 迁移并进入 `serve_forever`；自动恢复的后台／行业／AI任务均为 0。自动生成迁移前备份 `data/backups/pre-migration-20261014T053143244528Z.sqlite3`（schema 31，1,443,459,072 字节）；备份和迁移后原库均 `quick_check=ok`、外键错误 0，四表行数与原基线一致。原库 schema 32 文件 1,589,465,088 字节。后台进程保持在执行会话 97874 中运行。
- 用 Python 本机 HTTP 请求收到 WinError 10013；Codex 应用内浏览器访问 `http://127.0.0.1:8765/` 超时，维护 API 无法从当前执行环境完成实机验证。服务器启动日志报告页面 URL，但不能据此宣称 HTTP 页面/API 请求成功。另对已迁移的日常库直接调用只读 `MaintenanceService.read(refresh=True)` 成功，schema 32、68 张表、2 个 View、无未登记用途。实际启动信息及验收限制见 `data/verification/reports/20261014_financial_v32_8765_acceptance.json` 和 `data/verification/logs/daily-8765-financial-v32-20261014.*.log`。待用户从本机浏览器打开页面验证；P2 跨重启分页复用缺口仍未完成。
- 2026-10-14 用户反馈页面打不开后定位为服务运行于 Codex 隔离执行会话。停止隔离会话，在宿主侧隐藏启动 Python PID 8940（`app.py --port 8765`）；宿主确认监听 127.0.0.1:8765、首页 HTTP 200（1,803,165 字节）、维护 API HTTP 200（schema 32，68 张表、2 个 View）。后台继续运行。此前客户端中止探测在 stderr 留有一次 WinError 10053，后续首页/API 请求均成功。详见 `data/verification/reports/20261014_financial_v32_8765_acceptance.json`。

## 2026-10-14 · P2 缓存补齐与 8765 验收完成

- 补齐公共财务分页的同日跨 operation／进程重启复用：仅匹配同一数据集、精确请求参数 key、报告期、完整采集 scope、parser/definition/universe 版本及 UTC 同一天；候选原始归档在建立新来源引用前重新校验压缩文件与原文双哈希。复用记录使用 `request_status=reused`，保留真实 `obtained_at` 和 `reused_source_id`；归档不可读或哈希不符时回退到公共 RequestCollector 请求。没有修改事实表 schema 或触发数据库迁移。
- 新增隔离用例模拟两个不同 operation：首轮四表分页请求 4 次，第二轮跨 operation 全部复用、网络请求数仍为 4，原取得时间保持不变；用例通过。财务、行业财务 reader、索引采集和旧入口相关回归共 `71 passed`；`py_compile` 与 `git diff --check` 通过。pytest 临时文件均置于仓库隔离目录，完成后清理。
- 核对旧 8765 PID 8940 的可执行文件及完整命令行为 `python.exe -u app.py --port 8765` 后停止并隐藏重启；新 PID 42528 正监听 `127.0.0.1:8765`，后台保持运行。
- 重启后宿主本机 HTTP 验收：首页、维护统计、行业状态、贵州茅台行业数据、三表报表 API、行业与财报 JS/CSS 均 HTTP 200。行业数据返回非空，分类 universe/classified 各 5,223；财报期为 2026-06-30，fzb/lrb/llb 共 6 项展示数据。维护 API 返回 68 张表、2 个 View；已迁移日常库 schema 32。详情写入 `data/verification/reports/20261014_financial_v32_8765_acceptance.json`。
- 未执行真实网络采集、历史回填或定时任务；本轮 API 检查均为只读。
- 缓存审查补充：已完成批次第一页声明的页总数与该批解析完成的归档页集合必须一致；不一致时禁用整组复用并通过 RequestCollector 重采，防止页总数变化后混合旧新分页。
- 上述缓存完整性补充后再次运行同一组隔离回归：`71 passed`。再次核对进程身份后重启 8765，最终 PID 26976；首页、维护/行业/财报 API 及四个 JS/CSS 静态资源均 HTTP 200。`data/verification/reports/20261014_financial_v32_8765_acceptance.json` 已更新为最终进程及状态。
- 缓存专项用例还覆盖页总数变化：构造哈希有效但已完成批次页集合与第一页声明不一致的归档，验证缓存失效并重新请求；通过。修改后的完整 71 项回归再次通过。

## 2026-10-14 · 数据库文件增量核查

- 用户询问 v32 后数据库文件增大约 100 MB 的原因。只读比较迁移前备份与当前日常库：文件从 1,443,459,072 增至 1,589,465,088 字节（+146,006,016 字节，约 139.2 MiB）。两库 `collection_batches` 均 720 行、`collection_sources` 均 2,018 行；四张财务事实表及 `sw_memberships` 行数均不变，确认没有因本次接口切换采集新数据。
- SQLite 页统计：迁移前 page_count 352,407、freelist 33；迁移后 page_count 388,053、freelist 34,501。文件增量中约 141,180,928 字节（134.6 MiB）是四张财务表重建后留下的空闲页；非空闲页净增 4,825,088 字节（4.6 MiB），对应新结构及迁移相关占用。SQLite 默认保留空闲页供后续写入，不自动缩小文件。
- 核查只读完成，没有运行 VACUUM、写入或压缩日常库。克隆迁移逐列逐行哈希报告仍见 `data/verification/reports/20261014_financial_v32_full_clone.json`；日常库大小和 schema 32 验收见 `data/verification/reports/20261014_financial_v32_8765_acceptance.json`。

## 2026-10-14 · Sol 独立实现审查（需修正）

- 已核对本目录三个文件、Git diff、v32 引用迁移、采集／恢复／名单发布与读取路径。
- 现有财务、reader、指数采集、旧入口及成员测试重新执行：93 passed, 1 deselected；未运行 4,000 成员财务／市值联合用例，不把本轮结果视为全套测试通过。
- 补充隔离复现确认六项问题：R1 不同批次分页混用仍成功发布（P1）；R2 同日显式 recheck 不请求新来源；R3 连续名单换版本丢失上市日期来源；R4 有效 XLSX 冷却恢复误判；R5 新公共财务／分类批次中断后遗留 running；R6 新分类命令无法在首次建档时工作（后五项 P2）。
- 详细位置、复现与修复要求见本目录 REVIEW.md；机器可读复现结果见 `data/verification/reports/20261014_financial_public_review.json`。
- 审查结论：当前不能按“全部完成”验收，先修正上述问题并补回归。来源说明与 DECISIONS 中过期部署／缓存文字也需同步。
- 本轮仅更新审查资料；未修改业务代码、日常库或后台进程，未执行真实网络采集。隔离测试及复现临时库已清理。

## 2026-10-14 · Sol 审查 R1–R6 修正

- R1 分页一致性：只选择最新同日报告期第一页所对应的来源批次，并要求其归档页集合完整、连续、参数精确、逐页 count/pages/长度/字段/报告期/证券唯一性一致且双哈希有效。缓存不完整或无效时不从更旧批次补页，整段走公共 RequestCollector 重采。隔离回归模拟旧完整 501 家、较新中断页一组不同证券及重新采集，证明已发布证券全来自本次新请求，没有跨批次拼页。
- R2 显式复核：`industry_updates` 日常财务和 `market_financial_backfill.run` 历史 `--recheck` 均将 recheck 传入 `collect_public`；显式复核不查成功缓存。模拟值 0→2 后四段各触发修订，日常与历史入口均验证参数传递。
- R3 来源链：连续名单版本保留上市日期时，在当前 operation 对应交易所 batch 下创建 reused source 引用，保留原 `obtained_at` 和 `reused_source_id`。修复复用来源 attempt_no 碰撞，按目标批次及相同 request_key 递增。三次名单版本变化回归验证来源链连续且每版触发器关联有效。
- R4 XLSX 冷却探测：`_xlsx_health` 可接收 HTTP Response 并解析其 content。公共 collector 冷却到期后，有效 XLSX 清除冷却、无效工作簿继续冷却的测试均通过。
- R5 崩溃恢复：`recover_interrupted_runs` 按仍 running 的 sw_update_runs operation 关闭财务、行业分类和交易所名单公共 running batch；完成批次及其他 operation 的 running batch 保持原状态。隔离测试通过。
- R6 空库分类初始化：`ensure_daily_memberships` 允许空 `sw_imports` 库从公共名单/分类入口建档；不自动执行财务/历史采集。隔离测试验证分类发布成功、财务表仍为空。
- 更新财务来源说明、PLAN 部署状态、DECISIONS #11 和 REVIEW 的处理结论，避免遗留“不支持分页复用／未部署”的过期说明。
- 最终相关回归命令覆盖 `test_market_financial.py`、`test_industry_financial_reader.py`、`test_index_collection.py`、`test_collection_entrypoints.py`、`test_industry_memberships.py`：**99 passed**，包括 4,000 成员共享每日检查；六个改动模块及两组测试 `py_compile` 通过，`git diff --check` 通过。专项无真实网络采集、历史回填或定时任务。
- 最终宿主验收记录已同步至 `data/verification/reports/20261014_financial_v32_8765_acceptance.json`：记录 PID 29052、99 项相关回归及首页/API/静态资源 HTTP 200。进程在宿主运行，受限执行会话内的回环访问仍可能被 Windows 拒绝；此前宿主侧探测已成功，后台按规则保持运行。
- 扩展运行 `tests/test_industries.py` 后发现 6 项失败，未计入上述 99 项专项回归：两项本地 HTTP listener 在受限执行会话收到 WinError 10013；一项前端 Node 输出中文断言在 Windows 环境乱码；其余三项为名单版本日期断言及行业市值 fixture/聚合预期不一致。该文件超出 Sol 本次 R1–R6 指定回归集，尚未确认这些测试与本次修复存在因果关系；不宣称全仓测试通过。

## 2026-10-14 · Sol 第一轮复核 R7 修正

- 任务正常收尾原先整体替换 `sw_update_runs.result_json`，抹掉汇总发布事务写入的 `aggregation_results`。新增 `industry_updates.merge_run_result`，在 `perform` 自建任务及页面 `IndustryService._refresh` 两条完成路径合并任务结果，按季度保留已发布版本映射及此前结果／进度审计字段。
- `_refresh` 现在将自己建立的 `run_id` 显式传给 `perform`；汇总发布与 operation 的关系保持一致，避免收尾入口另行推断任务。
- 新增端到端回归分别调用自建 `perform` 和页面 `_refresh`：真实走 `_perform` 与 `aggregate_quarter` 发布；验证 run=complete、旧进度/审计字段和先前季度映射仍在、当前季度映射选择新 import，且页面 `total_cap=300` 与对应审计汇总完全一致。
- 验证：`tests/test_industry_cap_summary.py` 为 8 passed；行业季度市值集成筛选为 4 passed；合并五个既有专项回归文件及市值汇总文件为 **107 passed**（23.57 秒）。成功与失败两类任务入口均覆盖已发布分段映射保留。R7 验收只用隔离 SQLite，无来源网络请求。
- R7 复核复现报告已更新至 `data/verification/reports/20261014_financial_public_review_round1.json`。核对并隐藏重启宿主 8765，最终 PID 41352，命令为 `python.exe -u app.py --port 8765`。首页、维护统计、行业状态/数据、财报 API、行业/财报 JS/CSS 均 HTTP 200；启动日志为 `data/verification/logs/daily-8765-r7-20261014.stdout.log` 与 `.stderr.log`，详细验收写入 `data/verification/reports/20261014_financial_v32_8765_acceptance.json`。后台保持运行。
- 最终 R7 组合回归为 **107 passed**，`py_compile` 与 `git diff --check` 通过。测试使用隔离库；没有真实网络采集、历史回填或定时任务。此记录不代表全仓测试全部通过。

## 2026-10-14 · Sol 第一轮修复独立复核

- R1–R6 原复现场景修复成立；独立重跑五个专项文件为 **99 passed（21.76 秒）**。
- 继续追查行业扩展失败，确认新的 R7（P1）：`industry_updates.perform` 自建任务收尾，以及 `IndustryService._refresh` 页面任务收尾，会整体覆盖 result_json，丢失此前汇总发布保存的 aggregation_results，导致已发布行业市值汇总无法成为当前页面版本。
- 隔离证据：事实汇总已存 300，current_aggregation_versions 为空、页面为空；仅在临时库恢复该版本映射后页面变为 300。报告为 `data/verification/reports/20261014_financial_public_review_round1.json`。
- 一个名单/日期断言在换回 Git HEAD 原 `_import_bundle` 的内存对照中仍失败；市值用例中“新快照与旧市值混合补齐”的断言与当前汇总口径不符，但完整快照后的页面空值由 R7 确认，不能全部归为 fixture 问题或以不在专项集为由忽略。
- 本轮结论：六项修复可确认，整体仍需修 R7 并补完整任务入口回归，再更新验收结论。详见 REVIEW.md 最新章节。
- 仅更新审查资料，未修改业务代码、日常库或后台；无真实采集。测试与复现临时目录已清理。

## 2026-10-14 · Sol R7 修复独立确认（当前审查结论）

- 核对新增 `merge_run_result` 与自建任务／页面任务两条收尾，确认在同事务内合并原结果并保留 aggregation_results；失败后已提交映射也有测试覆盖。
- 独立重跑六个专项文件，**107 passed（24.12 秒）**。
- 另从 `perform`、页面 `_refresh` 两个入口重放原 R7 场景，真实经过 `refresh_quarter` 与行业汇总，只模拟网络和每日检查；两者任务均 complete，页面当前 import_id 与发布映射一致，审计及页面金额均为 300，无需手工修复。
- 独立报告：`data/verification/reports/20261014_financial_public_review_r7_final.json`。R1–R7 审查项均可关闭，本轮未发现新的阻塞问题。此前“仍需修 R7”的段落保留为历史记录，不代表当前状态。
- 本结论不宣称全仓测试或真实来源覆盖验收通过。本轮未修改业务代码／日常库／后台，未真实采集；临时库与测试目录已清理。
