# Sol 实现审查

日期：2026-10-14。结论：**需修正后再验收，当前不能标记全部完成。**

审查依据：本目录 PLAN／DECISIONS／PROGRESS、当前未提交 Git diff、schema v32 迁移与新旧来源读取代码，以及隔离测试。未改业务代码、未接触日常数据库写入、未启动真实网络采集或重启后台。

## 验证结果

- 重新运行 `test_market_financial.py`、`test_industry_financial_reader.py`、`test_index_collection.py`、`test_collection_entrypoints.py` 和 `test_industry_memberships.py`，排除 `finance_and_cap_tasks_share_one_daily_check`：**93 passed, 1 deselected**，21.16 秒。
- 补充六项隔离复现，全部使用临时库、假时钟或模拟响应；临时库退出后清理。结果保存在 `data/verification/reports/20261014_financial_public_review.json`。
- 通过的既有测试未覆盖下述边界。特别是现有缓存测试把“成功批次同日重复调用不请求”作为预期，不能证明 `--recheck` 真正获取修订，也没有覆盖不同批次页面混用。

## R1 · P1：分页恢复会拼接不同采集批次，仍通过校验并发布

位置：`quarterly_dashboard/market_financial.py:140–177`。

候选来源按每页 request_key 独立选择最新记录。第一页仅在候选批次为 complete 时核对该批页集合；之后各页没有固定到同一个候选批次／一致的快照。失败批次的第一页可以配上更旧 complete 批次的第二页，只要总数相等、证券不重复，就会通过发布。

隔离复现：旧完整批次返回 501 家；新批次第一页 500 家变成另一组证券，第二页失败；第三次恢复选中新失败批次第一页＋旧成功批次第二页。发布成功，统计 rows=501、new=500、unchanged=1、missing_previous=500。来源 operation 分别为 `operation:new-interrupted` 与 `operation:old-complete`。这些页面不是一次已经校验一致的分段，不能当作本轮全量事实。

修复要求：为整个数据集选择并绑定一组可验证的缓存批次／页集合；不得逐页从任意历史批次择新。缓存与新请求混合时需有明确的一致性策略，无法确认则整段失效并重采。增加上述“总数相同但证券集合变动”回归，不仅验证页总数变化。

## R2 · P2：显式重新核对也复用当天成功缓存，无法取到当天修订

位置：`quarterly_dashboard/market_financial.py:106,140–150`；调用方 `market_financial_backfill.py:112`、`industry_updates.py:191`。

`collect_public` 没有 recheck／缓存策略参数，也不限制只复用失败／取消的恢复来源；历史 `--recheck` 仅绕过已完成期跳过判断，仍会进入同日公共缓存。日常 recheck 同样未传给采集器。当天源新增披露、修订或从明确空结果变有值时，显式核对仍看见旧响应。

复现：第一次四表请求共 4 次并发布；模拟源数值由 1 变为 2；执行 `history.run(..., recheck=True)` 后网络总数仍为 4，revised=0，任务返回完成。

修复要求：区分常规增量、失败恢复和显式修订核对；recheck 强制经过公共调度获取新来源，不复用已成功响应。新增实际调用历史入口和日常入口的同日修订／空转非空测试。

## R3 · P2：名单换版本时丢弃上市日期公共来源，后续无法直接追溯

位置：`quarterly_dashboard/industry_service.py:438–456`，以及 `industry_memberships.py` 中保留旧上市日期的逻辑。

新公司首次取得上市日期后，事实引用公共 source。以后因名称、行业或成员变化创建新名单版本，旧 source 的 operation 与新 import 不同，于是代码移除成对引用，仅把旧引用放进当前 manifest。第三次名单变更时 foundation 只返回两列 NULL，无法再次传递旧出处；新 manifest 也不再有这条 retained 引用。上市日期数值仍保留，但当前事实失去可用来源关联。单纯要求引用必须来自当前 operation 与“保留原事实出处”目标相冲突。

复现：三次每日检查均完成，上市日期一直为 2010-01-01；第一天 public_source=3，第二天 source=NULL（manifest 暂存 retained），第三天两套 source 仍为 NULL 且 retained_refs=0。每次改变一个名称即可触发。

修复要求：保留可跨版本解析的原来源，或建立指向原版本／原来源的持久引用；如需当前 operation 中的复用 source，应明确 reused_source_id 和原取得时间，不能通过清空来源满足触发器。增加连续三次名单版本变化回归。

## R4 · P2：深交所有效 XLSX 的冷却恢复探测必然误判

位置：`quarterly_dashboard/industry_memberships.py:175–177`（`_xlsx_health`）。

`RequestCollector.get` 调用 `healthy_response(response)`；`_xlsx_health` 却把传入对象直接交给 `parse_szse`，后者需要 bytes，因此 valid Response 被捕获为 False。发生过深交所拒绝后，即使冷却已过且恢复响应为有效 XLSX，也会再次被判无效并延长冷却。

复现：同一测试工作簿，`_xlsx_health(bytes)=True`，`_xlsx_health(Response)=False`；真实公共 collector＋已到期冷却＋模拟有效 XLSX 返回 ProviderBlocked。

修复要求：回调读取 response.content，并通过公共 collector 验证有效／无效工作簿及冷却清除／延长，不只直接测试解析器。

## R5 · P2：后台恢复未处理新公共财务和分类批次

位置：`quarterly_dashboard/industry_service.py:217–228`。

恢复函数只处理公共 market.trade_snapshot、旧 market_financial_batches、每日检查与任务表。新 `market.financial.*`、分类及上市日期公共 batch 遗漏。崩溃后父 sw_update_runs 被标 failed，子批次却永久保留 running；下次恢复缓存还会把这些记录当候选，状态审计不一致。

复现：创建 running 财务 operation 与公共 income batch，调用 `recover_interrupted_runs`，父任务 failed、子 batch 仍 running。现有同名测试仅建立旧财务批次，未覆盖新路径。

修复要求：按本领域受中断 operation 关联关闭公共子批次，保留来源；不要影响其他领域仍在运行的任务。增加财务／分类／上市日期及已完成段不受影响的测试。

## R6 · P2：停用旧分类初始化后，新分类入口无法首次建档

位置：`quarterly_dashboard/industry_memberships.py:228–236`，及新分类 CLI。

旧 `industry_sources.download --foundation-only` 已停用并指向新命令；新命令最终调用 `ensure_daily_memberships`，却在下载前无条件 `service.foundation()`。新库没有 sw_imports 时直接失败，因此不存在能完成计划 P3“分类初始化”的新公共入口。

复现：已初始化 schema 32 的空业务库执行每日检查，status=failed，fetch_directory 调用次数为 0。

修复要求：区分首次分类建档与已有名单更新。首次只采并发布分类／名单和上市日期，不自动采财务或历史；同样经过公共调度、完整校验和任务互斥。

## 记录与验收范围

- DECISIONS 第 11 条仍写未部署／未迁移，已与 PROGRESS 后续生产部署记录冲突。
- 财务来源说明仍写“不支持公共分页跨进程复用”，且前文仍保留“字段补全直接更新当前记录／无变化更新来源”的旧语义，与现代码不符。
- 本审查未据文档声称“生产未出问题”或“生产数据受损”：部署后的真实公共财务与分类采集并未在本轮执行，发现来自隔离复现。
- 下一步应先修 R1，再修 R2–R6，并把对应复现纳入永久回归。完成后更新进度与来源说明，再按项目规则核对进程身份并部署验收。本审查没有代为修改业务实现。

## 复核处理结果 · 2026-10-14

初始审查记录的六项问题已按要求修正并纳入永久回归：

- R1：缓存只接受同一来源批次内完整、连续、逐页一致的一组归档；最新同日页组不完整或校验失败时整段通过公共 collector 重采，不从更旧批次补页。
- R2：`recheck` 已从日常财务任务及历史 `--recheck` 传入 collector，显式复核绕过成功缓存。测试验证同日来源值由 0 变 2 后发生修订，且两条入口都传递 recheck。
- R3：跨名单版本保留上市日期时，在当前 operation 的交易所批次下登记 reused 来源，原始取得时间及来源链连续三版保留。
- R4：XLSX 健康回调接受 Response 并校验 `content`；公共 collector 的冷却恢复测试覆盖有效文件清除冷却及无效文件继续冷却。
- R5：后台恢复关闭关联中的财务、分类、上市日期公共 running 批次；完成批次及其他 operation 不受影响。
- R6：空库可使用公共分类入口建立当前分类及名单，测试确认不生成财务事实。
- PLAN、DECISIONS、财务来源说明的部署状态及来源/补全/缓存语义已同步。

最终相关回归：`test_market_financial.py`、`test_industry_financial_reader.py`、`test_index_collection.py`、`test_collection_entrypoints.py`、`test_industry_memberships.py` 合计 **99 passed**（包含 4,000 成员共享每日检查用例）；相关 Python 文件 `py_compile` 通过，`git diff --check` 通过。无真实网络采集、历史回填或定时任务。

按项目规则完成复核后部署：先核实并停止旧 8765 `app.py --port 8765` 进程，再宿主侧隐藏启动新版；最终 PID 与页面/API 验收记录见 `data/verification/reports/20261014_financial_v32_8765_acceptance.json`。完整克隆迁移、日常库 v32 和原财务数据行数均未因本次 R1–R6 修复而改变。

## 第一轮修复的独立复核 · 2026-10-14

结论：**R1–R6 原复现场景的修复成立；整体仍需修正 R7 后验收。** 不能把文中“六项已修复”解释成全部相关行为已验收。

- 已核对六项代码改动与新增回归，独立重跑原五个测试文件：**99 passed，21.76 秒**，包含此前排除的 4,000 成员用例。
- 补跑行业集成文件（按关键词排除一个用例）得到 **27 passed、5 failed、1 deselected**。其中本机回环请求失败和 Node 中文输出问题属于环境表现；一个日期断言在仅将 `_import_bundle` 换回 Git HEAD 原实现的隔离对照中仍失败，不能归因为本轮名单修复。
- 其余季度市值用例不能仅以“不属于指定回归集”排除：已沿调用链定位到以下真实结果覆盖问题。一个用例仍期待“新快照＋旧市值”补成完整汇总，与现汇总仅用快照的口径不符，应更新测试数据；另一个完整快照发布后的页面空值由 R7 直接解释。

### R7 · P1：任务结束覆盖结果，已发布行业市值汇总失去当前版本映射

位置：`quarterly_dashboard/industry_updates.py:128–132`；页面任务收尾同样位于 `industry_service.py:262–264`。

`industry_cap_summary.publish_in_transaction` 已在 `sw_update_runs.result_json.aggregation_results[quarter]` 保存发布版本。`market_trade_snapshot.refresh_quarter` 返回的结果仅有单个 `aggregation` 字段；`perform` 的 owns_run 收尾直接用返回结果整体覆盖 result_json，清除了 aggregation_results。页面 `_refresh` 也采用同样覆盖方式。`current_aggregation_versions` 只读 aggregation_results，因此汇总行虽然已提交，页面却无法选择其版本，会回退到旧汇总或空值。

隔离复现：两家公司快照市值 100＋200，任务执行成功，`sw_industry_cap_quarters.total_cap=300`；任务结束后 `current_aggregation_versions={}`，页面 total_cap=NULL。仅在临时库中恢复该 operation 的 aggregation_results 后，页面立即返回 300，未改事实或市值。结果见 `data/verification/reports/20261014_financial_public_review_round1.json`。

修复要求：任务完成时在事务中合并现有 result_json，保留汇总已发布的版本映射与审计字段；同时修复自有 run 的 perform 与页面 `_refresh` 两条收尾路径。取消／失败但已提交分段的元数据也不得被后续进度覆盖。增加端到端回归：通过完整任务入口发布后，验证 run 状态完成、aggregation_results 保留、current_aggregation_versions 选中新版，以及页面数值与对应审计汇总一致。不要只测试 aggregate_quarter 单函数。

本轮未修改业务实现、日常库或后台，未执行真实来源请求。审查用临时库与测试目录已清理。

## R7 复核处理结果 · 2026-10-14

- 修复 `industry_updates.perform` 自有任务与 `IndustryService._refresh` 页面任务两个完成收尾点：完成结果现在合并到已保存的 `result_json`，`aggregation_results` 以季度为键合并，保留汇总发布元数据和原任务进度／审计字段；页面任务将已创建 `run_id` 显式传入 `perform`。
- 新增完整任务入口隔离回归。覆盖 `perform` 和页面 `_refresh` 成功收尾，断言任务状态完成、此前的进度/审计字段及其他季度汇总映射未丢失、`current_aggregation_versions` 指向本次汇总，且页面所选版本与审计总额均为 300；也覆盖汇总已提交、后续步骤失败时两个入口均保留部分发布映射。
- 复测 `tests/test_industry_cap_summary.py`：8 passed；行业市值集成相关筛选：4 passed；结合财务、来源及旧入口专项测试：107 passed。`data/verification/reports/20261014_financial_public_review_round1.json` 已加入修复与回归证据。
- R7 代码及回归复核通过。随后按项目规则核对旧进程并隐藏重启 8765，最终 PID 41352；首页、维护统计、行业状态/数据、财报 API 和行业/财报 JS/CSS 均 HTTP 200，后台保持运行。最终验收与启动日志分别见 `data/verification/reports/20261014_financial_v32_8765_acceptance.json` 和 `data/verification/logs/daily-8765-r7-20261014.*.log`。
- R7 最终组合回归 **107 passed**，静态检查通过。审查确认 R1–R7 的代码问题均已有回归覆盖；仍不据此宣称全仓测试通过，也未运行真实采集、历史回填或定时任务。

## R7 修复后的独立确认 · 2026-10-14

结论：**本次 R1–R7 审查项均可关闭；本轮未发现新的阻塞问题。** 以本节覆盖前文历史上的“R7 待修”状态。

- 已检查 `merge_run_result` 在原事务内读取并合并保存结果，并核对自建 `perform` 与页面 `_refresh` 两条成功收尾路径均调用该函数；失败路径继续保留已提交的结果。
- 独立重跑六个专项文件，结果 **107 passed（24.12 秒）**，包括两个入口的成功／失败、既存季度映射与进度审计字段保留。
- 另将上轮原始复现场景分别从两个完整入口重放，使用真实 `refresh_quarter`、真实汇总及页面读取，只模拟来源网络与每日检查。两条路径均为任务 complete、当前版本映射与页面 import_id 一致，审计汇总及页面 total_cap 均为 300；没有手动补回映射。隔离证据为 `data/verification/reports/20261014_financial_public_review_r7_final.json`。
- 本结论限于已审查修改和上述回归；不代表全仓测试通过，也不代替真实来源覆盖验收。生产重启及 HTTP 200 是执行记录中的验收，本轮未再次操作日常后台。
- 本轮只追加审查资料，未修改业务代码、日常数据库或后台；未执行真实采集，临时库与测试目录已清理。
