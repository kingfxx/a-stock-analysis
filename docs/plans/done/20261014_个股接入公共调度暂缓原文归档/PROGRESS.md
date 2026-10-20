# 进度

## 2026-10-14 · Sol 规划

- 已阅读 AGENTS、采集规则和财务规则；复核公共 collector 的锁／预算／归档耦合，个股服务入口、浏览器加载依赖、分页与补偿请求、sync_runs/state 及前复权版本职责。
- 已确认用户授权边界：保留页面按时间自动检查，估值／融资日更；先接公共调度和元数据记录，原文文件／压缩包／BLOB 归档暂缓。
- 已编写本目录 PLAN.md、DECISIONS.md：公共层显式 metadata_only、五个领域服务与辅助请求接入、来源映射、提交／中断恢复、更新策略保留、隔离测试和部署验收。
- 当前只新增本目录三个 Markdown 文件，未修改业务代码、规则文件、数据库或后台；未运行采集、测试、备份、迁移或重启。
- 当前仓库有此前财务／行业整合未提交改动；执行时须保留，不纳为本计划进度。

## 状态

P0–P5 已实施；S1–S4 均已修正、通过模拟隔离回归并部署。组合回归有 3 项 live HTTP 测试未运行，且未做真实来源覆盖验收。本计划不隐含旧历史来源迁移、响应归档或定时任务。

## 2026-10-14 · Luna 执行

- 已按执行要求重新读取本计划三个文件、根 AGENTS、`docs/agent-rules/collection.md` 及财务/财报规则对应章节；已读取 Karpathy 编码规范。确认执行授权覆盖原计划“仅规划”状态，允许按阶段实现和隔离测试；不运行真实来源请求、不回填、不迁移原库、不覆盖既有未提交工作。
- P0 已完成代码链路清单：页面并行/串行依赖、四套财务类型、股东两来源、融资、分红窗口及补偿、百度多窗口、东财行业快照、腾讯名称、价格分页/锚点/复权核对和各自真实请求触点均记录于 PLAN.md。新增请求频率基线，区分 24 小时、上海自然日、5 秒合并及 full/audit 策略；标出 ProviderBlocked 可能被补偿分支吞掉的风险。
- P1 公共层基础已实现：`RequestCollector.get(..., evidence_mode='metadata_only')` 显式不落盘、不计算原文哈希，仍使用现有 provider 锁、预算、拒绝冷却及逐请求登记；成功响应的 parse_status 保持 pending 等领域校验结束后 finalize。默认 `archive` 路径未改。新增 `tests/test_stock_collection.py` 两个隔离测试。
- 验证：`python -m pytest -q tests/test_stock_collection.py` 通过（2 passed）。曾尝试与既有 `tests/test_index_collection.py` 回归；26 项失败均在 `atomic_write` 创建临时数据库旁 `collection_sources` 文件时遭 Windows sandbox `FileNotFoundError`，非断言失败；6 项通过。`pytest` 入口命令本身无法导入本地包，改用 `python -m pytest` 正常运行。
- 尚未将业务 Session 路由到公共 collector，也未创建个股批次、领域 parse finalize、补偿拒绝传播、更新等待或展示逻辑；故 P1–P5 未完成。collection.py 原有未提交的 reusable_source / 401 冷却改动保留，本次只在其上增加参数和归档条件。无外部来源请求、无原库写入、无服务启动/重启。
- 因剩余范围涉及 5 个服务、stock sync_run 与公共 batch 的原子完成/失败恢复、拒绝穿透多种补偿分支、页面等待与完整隔离回归，当前阶段仅交付完成的 P0 和可测试的 P1 公共能力，不声称全计划完成。继续实施前须在执行宿主提供可写隔离临时目录，以运行完整公共 collector 回归；8765 仍待全计划相关代码测试后按规则核对并重启。

## 2026-10-14 · Luna 执行续记

- P1 完成：新增 `stock_collection.py` 元数据批次与 Session 路由；`network.create_data_session` 仅在显式 stock route 下代理请求。metadata_only 不保存响应正文／原文哈希；完成领域解析后才 finalize 来源解析状态。默认归档路径保持原样。
- P2–P3 完成：东财股东、融资、分红、行业快照；新浪财务；百度估值；腾讯 raw/qfq 价格与简称请求均接入同一 provider 调度。拒绝异常保留为阻断，不进入同源补偿；更新既有业务同步频率、数据身份及价格版本校验。
- P4 完成：sync_run 与 stock batch 完成／失败在同一短事务关闭；启动恢复只关联并关闭 `domain=stock` 的中断批次。增加自动失败后 600 秒静默间隔，手动刷新可绕过；补足页面全量按钮恢复和缓存加载失败提示测试。文档已同步到采集规则、SQLite 存储、维护说明和 `docs/stock-collection.md`。
- 隔离回归：`python -m pytest -q --basetemp=data/verification/.tmp_stock_collection tests/test_stock_collection.py tests/test_index_collection.py tests/test_fundamental_service.py tests/test_dividend_service.py tests/test_chips.py tests/test_valuation_service.py tests/test_valuation_frequency.py tests/test_valuation_transport.py tests/test_storage.py tests/test_storage_startup.py tests/test_storage_source_contracts.py tests/test_collection_entrypoints.py tests/test_p4_source_contracts.py tests/test_async_loading.py -k "not live_routes and not live_data_failure and not live_shared_price_context"` 通过，219 passed，3 deselected。三项被排除的本机 HTTP listener 测试此前确认因 sandbox `WinError 10013` 无法绑定／连接。`python -m compileall -q quarterly_dashboard app.py` 和 `git diff --check` 通过；测试临时目录已清理。
- P5 阻塞：检查时 8765 无可见监听。按规则先确认进程身份后才尝试启动；启动被数据库实例锁拒绝（`StorageError: Another application/maintenance instance is using this database`，lock 文件最后修改于 2026-09-30 20:08）。进程信息查询被 sandbox 拒绝，故无法安全识别锁持有者；没有删除 lock、终止进程或访问可能写入业务状态的 API。未能完成 8765 页面/API 冒烟验收，需在可确认该数据库锁持有者与进程管理权限的宿主上完成。
- 2026-10-14 续办 P5：复查时 8765 无可见监听，`Get-Process` 未发现 Python 服务；对 `DEFAULT_DATABASE` 的实例锁做了短暂获取/释放探测，确认当时锁可用（旧 `.lock` 文件是持久文件，不代表锁仍持有）。按规则启动 `python -u app.py --port 8765` 后，启动输出为 SQLite 3.45.1、journal mode `delete`、schema 32，恢复中断任务／AI／行业任务均为 0，并进入 `http://127.0.0.1:8765/` 服务循环；之后同一实例锁探测被拒，说明该运行实例持锁。PowerShell 本机 HTTP 访问返回 Winsock access denied，Codex 浏览器访问超时；因此不能声称首页或 API HTTP 验收通过。用户附图提到另一轮曾在宿主侧运行 PID 8940 并验收 HTTP 200，但本轮无法核实该 PID 或继承那一轮的运行状态。为避免隔离实例与宿主实例争用原库，已结束本轮自己启动的隔离服务；随后实例锁可用，Codex 浏览器返回 connection refused。当前 P5 需在可访问宿主回环网络的执行环境启动/核验 8765。
- 最终重启：释放本轮隔离实例后，经宿主侧后台启动 `python -u app.py --port 8765`，PID **35192**（`python.exe`）。宿主检查确认监听地址 `127.0.0.1:8765`，数据库实例锁由服务持有；启动日志 `data/verification/logs/daily-8765-stock-collection-20261014.out.log` 显示 SQLite 3.45.1、journal mode `delete`、schema 32，恢复中断同步／AI／行业任务均为 0；stderr 日志为空。首页 `GET /` 返回 **HTTP 200**（1,803,201 bytes），只读维护统计 `GET /api/maintenance/storage` 返回 **HTTP 200**（25,432 bytes）；Codex 浏览器打开首页成功，页面可见股票 601919 与企业经营趋势。后台保持运行，未执行任何个股采集。
- Sol 只读审查发现来源页级 `parse_status` 与跨页／业务候选校验的边界未写清。已澄清于 `docs/stock-collection.md`：来源解析状态仅描述单次响应页级解码／解析；后续批次校验失败通过 batch 失败状态表达，sync_run 不提交事实。该状态语义不宣称整批业务校验成功。
- 针对该边界新增 `test_page_parse_status_does_not_claim_batch_validation_success`，隔离运行 `python -m pytest -q --basetemp=data/verification/.tmp_stock_review tests/test_stock_collection.py`：7 passed；`git diff --check` 通过，临时测试目录已清理。
- 本次未执行真实来源请求、全量回填或旧数据迁移。工作树中原有财务／行业改动保留；没有改动其他计划的进度文件。

## 2026-10-14 · Sol 独立验收（待修正）

- 已核对本目录三个文件及实际实现，确认 metadata_only、原业务 raw_json／同步控制保留、公共预算接入等主要范围符合计划。
- 依执行记录重跑组合：**220 passed、3 deselected（27.91 秒）**；三项本机 HTTP 测试未执行，不宣称全仓或真实来源验收完成。
- S1（P2）：来源健康探测过于宽松。隔离模拟百度 Result=[{}]，PE 业务解析失败前冷却已清除，随后 PB 又实际发送。应以接口可用性验证通过后才解除冷却，无效探测继续阻断。
- S2（P3）：recent_sync_failure 仅查询近期失败，后续成功／no_data 不解除旧静默。隔离建立 failed run 后完成 no_data run，仍返回旧失败；会导致不准确的暂缓警告。
- 详细位置和修复／测试要求见本目录 REVIEW.md，复现报告为 `data/verification/reports/20261014_stock_collection_review.json`。
- 本轮仅更新当前计划的审查资料，未修改业务代码、原数据库或后台，未执行真实网络采集。临时库和测试目录已清理。

## 2026-10-14 · Luna 按审阅修正

- S1 已修正：恢复探测按当前来源接口校验响应结构与业务身份。百度要求匹配当前指标／窗口的有效图表；腾讯行情复用日线解析器核对请求股票和复权字段，简称响应复用名称解析器；东财校验明确无数据协议、分页数量／页码、股票代码及筹码字段；新浪校验报告数与当前分页记录数。非法顶层 JSON、缺节点及结构异常安全判无效，公共层保持冷却并阻止同轮后续请求。
- S1 隔离回归覆盖百度无效结构后阻止第二个指标请求、百度有效图表恢复、东财非法股票节点／分页、腾讯缺请求股票节点、新浪 report_count/list 不一致。均通过 `RoutedSession` 和实际解析入口模拟，无真实请求；另有东财明确 9201 无数据分支仍由既有业务解析处理。
- S2 已修正：按同一 SyncKey 最近完成时间判断，失败后 success／no_data／superseded 会解除旧失败静默；最近一次仍为 failed 且在 600 秒内才继续静默。补充 no_data 与 superseded 回归。
- 聚焦测试 `python -m pytest -q --basetemp=data/verification/.tmp_stock_review_next tests/test_stock_collection.py`：**14 passed**（本轮扩充后由组合结果确认）。
- 按 REVIEW.md 原组合运行：**227 passed，3 deselected**（live_routes、live_data_failure、live_shared_price_context）；`git diff --check` 通过。临时测试目录已清理；没有真实来源请求、原库写入或历史回填。
- 已核对旧 PID 35192 命令行为本项目 `python.exe -u app.py --port 8765` 后重启；新宿主进程 PID **39132**，监听 `127.0.0.1:8765`。启动日志确认 SQLite 3.45.1、journal mode delete、schema 32，恢复任务均为 0；首页 **HTTP 200**（1,803,201 bytes），`/api/maintenance/storage` **HTTP 200**（25,432 bytes）。后台保持运行，未执行个股采集。
- 启动轮询时曾有一次短超时探测关闭大页面连接，stderr 记录 `ConnectionAbortedError 10053`；随后延长超时的首页与 API 请求均返回 HTTP 200。错误对应客户端中止，不是服务启动或数据库错误。
- 至此 S1/S2 修正已部署并完成人工访问/API 冒烟；P0–P5 阶段记录保留，最终审查边界仍为组合中 3 项 live HTTP 用例未运行、未做真实来源覆盖验收。

## 2026-10-14 · Luna 修正 S3/S4

- S3：腾讯简称 URL 的 `q=` 位于 path，健康检查现在从真实 `q=sh/sz######` 协议提取代码并复用 `parse_stock_name`；正确股票可恢复，错误市场代码仍阻断。
- S4：腾讯 qfq 恢复响应若有 `qfqday`，复用 qfq 价格解析直接确认；若只返回 `day`，将恢复标记和唯一 token 暂存在公共 provider 状态，保持冷却。raw、hfq 同窗请求继续各自经过 RequestCollector 和共享预算；只有两次价格均有效且窗口完全一致才解除冷却。错误、异常或不一致会延长冷却；另一 collector 不能越过待验证状态，超过 5 分钟的遗留验证标记可重新探测。
- 新增恢复回归：简称正确／错误代码；qfqday 直返；day＋raw/hfq 一致；窗口不一致继续冷却；第二 collector 不得解除待验证状态；另覆盖新浪和东财有效恢复及东财明确无数据协议。
- 针对测试 **23 passed**；按审阅组合扩大到来源／价格／公共调度回归：**258 passed，3 deselected**；`python -m compileall -q quarterly_dashboard app.py` 与 `git diff --check` 通过。隔离目录已清理，无真实请求。
- 已核对旧 PID 39132 命令行为本项目 `python.exe -u app.py --port 8765` 后重启；新宿主进程 PID **31336**，监听 `127.0.0.1:8765`。启动日志确认 SQLite 3.45.1、journal mode delete、schema 32，恢复任务均为 0；首页 **HTTP 200**（1,803,201 bytes），`/api/maintenance/storage` **HTTP 200**（25,432 bytes），stderr 为空。日志：`data/verification/logs/daily-8765-stock-review-fix2-20261014.out.log`、`.err.log`。后台保持运行，未执行个股采集。
- 至此 S1–S4 已部署；最终限制仍为 3 项 live HTTP 用例未运行，且无真实来源覆盖验收。

## 2026-10-14 · Sol 修正后独立复核（待修 S3/S4）

- 独立重跑原组合：**227 passed、3 deselected（27.46 秒）**。确认 S1 原百度无效结构不再解除冷却，S2 旧失败在后续成功／no_data 后解除。
- 新增隔离有效响应复现发现 S3（P2）：腾讯简称 URL 的 path 最后一段含 q=，健康回调把它一并作为 symbol，导致真实有效报价在恢复探测时必然被拒，延长 tencent 冷却。
- S4（P2）：健康回调直接按 qfqday 解析，阻断原 `fetch_price_history` 支持的 day＋raw/hfq 核对分支。同一模拟响应普通请求三次核对成功，冷却到期时第一次即误报 ProviderBlocked。
- 详见本目录 REVIEW.md 最新章节与 `data/verification/reports/20261014_stock_collection_review_fix1.json`。S3/S4 需增加各自有效恢复与无效阻断回归，修正后再关闭最终验收。
- 本轮仅更新本计划审查资料，未修改业务实现、日常库或后台；无真实来源请求，临时库和测试目录已清理。

## 2026-10-14 · 002463 页面报错只读诊断

- 用户报告名称获取失败后，只读核对 8765 日志及原库 `collection_provider_state`／`collection_sources`。`002463` 当次先请求 Tencent raw 日线；HTTP 200 响应被公共拒绝规则识别为验证码／反爬响应，随后 `tencent` 全 provider 冷却至 `2026-10-14T10:34:30Z`（上海时间 18:34:30）。股票名称也使用同一 provider，因此正确地被冷却挡住；不是简称 URL 解析回归。
- 该路径仅登记 metadata，原响应正文没有归档，无法进一步判定具体拦截页内容。未清除冷却、未重试或借其他来源绕过；到冷却结束后，下一次用户页面请求会按公共机制单次探测恢复。原库中 `002463` 的 name 仍为空。

## 2026-10-14 · Tencent JSON MIME 误判修复

- 单次用户授权诊断结果：002463 腾讯接口响应 HTTP 200、43,339 字节，可由 JSON 解码为对象，响应头却为 `text/html; charset=UTF-8`；没有命中验证码／频率限制文本标记。公共层仅因 MIME 标记将其拦截，确认存在 JSON MIME 误判，但由于本轮遵守 metadata_only，未保存 JSON 字段内容，不能证明业务数据结构本身有效。
- 修正 `RequestCollector.get`：对 `expect=json` 先尝试 JSON 解码；正文是有效 JSON 时不再仅因 `Content-Type: text/html` 拦截。正文确实以 HTML 开始或 MIME 为 HTML 且 JSON 解码失败时仍拦截；HTTP 401/403/429 和反爬标记逻辑不变。
- 新增有效 JSON + text/html MIME 放行、HTML 正文 + JSON MIME 仍拦截两项回归测试。`tests/test_index_collection.py tests/test_stock_collection.py`：**55 passed**；`compileall` 与 `git diff --check` 通过。首次测试曾因系统 pytest 临时目录权限失败，改用仓库内隔离临时目录后通过。
- 8765 当前无监听进程，待按项目日常后台规则启动并检查首页／维护 API；本轮不再访问腾讯。日常库 Tencent 冷却仍保留至 `2026-10-14T11:41:39Z`（上海时间 19:41:39），预算及拒绝次数未清除。
- 已重启宿主 8765：旧 PID 31336 身份核验后停止，新 PID 41144；启动日志正常，首页 HTTP 200（1,803,292 字节），`/api/maintenance/storage` HTTP 200（25,433 字节），stderr 无内容。没有执行第二次腾讯请求；真实库冷却保留。

## 2026-10-14 · 个股请求恢复旧式快速节奏

- 用户确认希望个股首次采集接近旧体验。个股 RoutedSession 请求现在向 RequestCollector 明确传入 `stock_fast=True`：同提供方跨线程／跨进程锁仍串行，来源拒绝及冷却仍生效，所有请求仍逐次记入共享 `budget_json`；仅个股路径跳过 `ready_at`、滚动 60 秒次数等待和每 20 次休息。非个股调用未改变；其限速仍基于共享窗口读取个股请求时间。
- 新增隔离测试：6 条个股请求按网络响应耗时串行、未等待公共间隔且全数计数；随后非个股请求仍按共享 60 秒窗口等待。`tests/test_stock_collection.py tests/test_index_collection.py`：**56 passed**；编译检查、diff 检查通过。
- 同步更新根 AGENTS、采集规则和个股采集说明，明示个股速度例外及其他采集器可能因同源个股流量等待窗口恢复。未执行真实腾讯请求。
- 变更后按 AGENTS 核对并重启日常后台：旧 PID 41144 已验证为本项目 `app.py --port 8765` 后停止，新 PID 30404 持续运行。首页 HTTP 200（1,803,339 字节），只读 `/api/maintenance/storage` HTTP 200（25,433 字节），启动日志正常；未发起真实来源请求。
