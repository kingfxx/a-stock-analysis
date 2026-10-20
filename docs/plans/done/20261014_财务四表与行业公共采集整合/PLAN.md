# 财务四表与全行业公共采集整合计划

日期：2026-10-14。阶段：Sol 独立审查后修正与最终验收中。用户已授权按本计划实施并要求重启后台继续验收；schema v32 已迁移到日常库，8765 后台已保持运行。真实采集、历史回填及定时任务仍不在授权范围内。Sol 审查缺陷及逐项处理见同目录 `REVIEW.md` 与 `PROGRESS.md`。

## 1. 目标与边界

把全市场财务四表和行业分类／成员链路的新增请求接入 `quarterly_dashboard/collection.py`，统一来源证据、跨进程预算、拒绝冷却、分段恢复及覆盖统计。保留四张财务业务表、行业业务表与现有页面财务口径；复用 `sw_update_runs` 管任务，不新建财务或行业专用 operations/batches/sources 控制体系。

公共采集整合不等于把业务事实塞入公共表。`sw_` 是业务命名前缀，不是提供方；公司申万分类与申万指数身份继续分开。旧事实、旧来源文件和历史任务不自动迁入、不删除、不回写。定时采集、新增历史范围和大规模重采不包含在本计划授权内。

## 2. 已核实的当前实现

依据根 AGENTS.md、`docs/agent-rules/collection.md`、`docs/agent-rules/finance-and-reports.md`、迁移 SQL 及以下代码。日常数据库仅通过 SQLite `mode=ro` 读取，未调用初始化、备份、采集或迁移函数。

### 2.1 财务四表

| 业务表 | 东财接口 | 入口与现状 |
| --- | --- | --- |
| market_financial_income | RPT_DMSK_FN_INCOME | 日常 industry_updates.perform → market_financial.collect/publish |
| market_financial_balance | RPT_DMSK_FN_BALANCE | 同上 |
| market_financial_cashflow | RPT_DMSK_FN_CASHFLOW | 同上 |
| market_financial_performance | RPT_LICO_FN_CPD | 同上；报告期源字段 REPORTDATE，其余三表 REPORT_DATE |

- 四接口均 `columns=ALL`、每页 500，源范围为全 A 股含北交所；行业展示范围仍为沪深名单。
- `market_financial_backfill.run` 复用同一采集器，按报告期从新到旧提交；日常财务更新前调用 `ensure_daily_memberships`，历史财务不更新分类和市值。
- 一个报告期四接口全部校验后同事务发布。已有同报告期行数骤减超过 20% 阻止发布；明确 9201 空结果与一般异常有区分。
- 独立使用 `market_financial_batches/sources` 和数据库旁 `market_financial_sources/`；仅失败同日检查点复用，成功清检查点。来源表是在最终发布事务才登记，失败尝试及中途成功页不能完整从数据库追溯。
- `industry_bulk.RequestPacer` 为进程内计数，2.5—3.5 秒间隔、20 次休息 30 秒；没有与公共 eastmoney 状态共享预算。403 等可能进入本地普通重试，不符合当前公共拒绝规则。
- `publish` 对无财务修订记录仍改写 source_id/batch_id；字段由空补全直接更新当前记录，有值变空会作为修订发布。接入时须处理来源保持、字段补全追溯及撤回确认，不能只替换网络调用。
- `IndustryService._load` 的缓存版本和页面财务更新时间查询旧财务批次；`IndustryFinancialReader.provenance` 直接查旧来源表。必须同步改造，否则新公共发布不会正确刷新或展示来源。

### 2.2 全部 14 张 sw_ 表的处理分类

| 表 | 实际角色 | 整合处理 |
| --- | --- | --- |
| sw_industries | SW2021 一二三级目录，511 行 | 保留；目录文件请求接公共采集，发布关联到导入清单 |
| sw_memberships | 版本化沪深成员及上市日期 | 保留；成员与上市日期来源接公共采集 |
| sw_membership_history | 来源提供的分类变更记录 | 保留；随名单版本发布，不冒充完整历史全市场名单 |
| sw_listing_sources | 旧上市日期共享证据 | 保留旧引用；新上市日期直接引用公共来源，不再复制原文元数据 |
| sw_membership_checks | 上海自然日检查控制与结果 | 保留去重控制，关联当前 operation 和公共批次 |
| sw_imports | 名单导入、旧数据及汇总审计版本 | 保留；新清单保存公共批次／来源引用，不另存原文路径哈希副本 |
| sw_update_runs | 行业及财务任务控制、互斥与结果 | 继续作为 operation 控制；所有新采集显式传入 run_id |
| sw_cap_quarter_rosters | 冻结季度、交易日、名单版本 | 保留，不随分类更新改写冻结口径 |
| sw_cap_quarter_members | 冻结季度逐公司行业归属 | 保留，无独立网络采集 |
| sw_industry_cap_quarters | 三级行业季度市值及覆盖汇总 | 保留；复用已接公共框架的交易快照输入和现有汇总审计 |
| sw_financial_facts | 旧行业财务及修订 | 只保留历史；日常与历史新财务仍不写本表 |
| sw_financial_provenance | 旧财务集中证据 | 保留，不为新财务双写 |
| sw_cap_facts | 旧公司市值、历史补缺事实 | 保留兼容／追溯，不恢复为日常写入目标 |
| sw_cap_provenance | 旧市值集中证据 | 保留，不为新快照双写 |

分类链路：`industry_memberships.fetch_directory` 请求上交所主板 JSON、科创板 JSON、深交所 XLSX、申万 taxonomy.xls、members.xls。现为裸 Session.get + 1 秒等待；申万文件与已接入的指数请求应共享既有 `sws` 提供方身份，不能新建 `sw` 预算。

`ensure_daily_memberships` 当前成功／失败／中断当天均不自动重试；失败沿用本地名单并提示。缺失公司暂保留旧成员，缺行业会沿用旧行业。整合时必须明确标记“本地保留”，不能把合并后的名单整体记为当日来源直接返回，也不能据此宣称历史完整。

其他入口：`industry_sources.download --foundation-only` 仍有旧分类下载（申万文件 + 新浪沪深名单）；非 foundation 模式含旧试点财务／市值；`industry_bulk.collect` 已显式停用。`industry_financial_extensions`、`industry_performance_backfill`、`industry_cap_history` 等仍有独立旧网络路径。须给出入口处置，不能留下可绕开预算的批采。`industry_position` 的个股业务请求不属于本次 sw_ 集采范围。

### 2.3 本机只读基线（非逐指标完整性验收）

- schema 31；四表均为 2014-09-30—2026-06-30，共 48 个报告期。
- income 226,479；balance 219,142；cashflow 226,153；performance 225,630 行，含版本。
- 旧财务批次 49、旧财务来源 1,945；公共批次 720、公共来源 2,018。
- 公共批次已有指数、交易快照、合成输入等数据集，尚无四表及公司分类成员数据集。
- sw_imports 135；sw_industries 511；sw_memberships 20,892；sw_membership_history 46,428；sw_listing_sources 3；sw_membership_checks 1；sw_update_runs 23。
- sw_cap_quarter_rosters 47；sw_cap_quarter_members 226,977；sw_industry_cap_quarters 18,359；sw_cap_facts 174,410；sw_cap_provenance 21,363；sw_financial_facts 188,784；sw_financial_provenance 8,720。

这些是读取时点快照。48 期有记录不表示每家公司每个字段齐全；本阶段没有重验 1,945 个原文文件。

## 3. 目标采集模型

### 3.1 operation／batch／source

- operation：沿用 `sw_update_runs`；`operation_id=sw_update_runs:<id>`。分类前置检查与主财务／快照任务共享本次 operation，不靠“取最新 running 任务”推断关联。独立显式分类更新也先取得同一互斥控制。
- 财务 batch：每“报告期 × 四表之一”一段，建议 `market.financial.income/balance/cashflow/performance`，provider=eastmoney，frequency=quarterly。一个报告期四段验证完成后仍一起发布；四段完成状态和事实同事务提交。
- 分类 batch：建议 `industry.sw.taxonomy`、`industry.sw.memberships`（provider=sws），`market.listings.sse`（主板／科创板两请求）、`market.listings.szse`（provider=sse/szse）。全部依赖验证后发布一个名单版本；已取得原文保留以便恢复。
- source：每实际请求／重试登记；二进制 XLS/XLSX 使用公共归档，先校验拒绝、文件签名与可解析性再标 parse complete；为恢复探测提供对应 healthy_response，不能套 JSON 探测。
- scope 包含报告期或上海检查日、SW2021、市场范围、接口／板块、ALL 字段、名单版本、解析／定义版本；幂等键同样覆盖这些身份。统一显式传 parser_version，避免继承公共函数的 index-v2 默认值。

### 3.2 同源保护

所有新增请求通过 RequestCollector；eastmoney 与交易快照共享，sws 与申万指数共享，交易所按真实独立提供方 sse/szse。保留 SWAdapter 的 TLS 适配，不绕过 TLS 校验。若保留新浪名单，则复用 sina，不新拆预算。

沿用公共默认：并发 1、完成后 10 秒＋0—5 秒、60 秒至多 5 次、每 20 次休息 60—120 秒，跨进程／重启共享。拒绝立即终止该来源本轮请求、冷却至少一小时；到期单次健康探测。普通临时错误最多三次、60／120 秒＋随机退避。解析错误只归档和失败，不反复请求。测试前审计公共 get/invoke 与此契约的差异，必要修补集中在公共层。

仅操作已授权范围，不把跨来源失败变成自动扩大抓取。分类前置失败可继续使用现有名单并报告陈旧程度；同源 halted 状态必须在整轮 operation 中保留，不能新建 collector 消除本轮停止状态。

### 3.3 发布、修订与恢复

- 财务保留 ALL、字段映射、报告期／交易所／A 股校验、总数稳定／每页长度／跨页唯一性及骤减保护；请求、等待和原文落盘不占 SQLite 写事务。
- 同报告期四表通过后短事务发布。任一失败该报告期不发布；其他已提交报告期保留。已校验原文页可恢复复用，parse complete 不等于事实已发布。
- 恢复键校验准确参数、版本、范围、同日缓存策略与双哈希；复用保留原 obtained_at 和 reused_source_id，分页总数或口径变化使缓存失效。基于公共来源恢复，停止新写旧 JSON 检查点；旧检查点不自动视为公共来源。
- 相同业务值不新建事实版本、不改写原来源；新请求仍单独留在公共来源与核对结果中。字段缺失转有效值是内容变化，应产生可追溯修订；描述字段变化可留核对记录，不能悄悄换掉原出处。有值变空先标疑似撤回、保留原有效值并报告待核实，不静默清空。
- 明确空结果标 empty，不能以“曾执行 complete”永久跳过。常规补新先查各数据集水位；已最新不请求。四表修订核对通过独立显式 recheck，初版不增加自动修订窗口。历史命令按完整同范围段跳过，明确空／未完成／已识别缺口进入待补，内部缺口复查与补尾部区分。
- 分类未变不复制成员版本；新请求与旧版本复用关系仍记录。上市日期更新独立追溯；旧值保留／来源暂缺要报告真实合并方式。

### 3.4 覆盖

分别记录请求与执行状态、来源分页数量、验证行数、发布行数、逐字段有效／NULL 数、公司缺行、旧记录缺失、明确空响应。全 A 股来源覆盖与沪深行业展示覆盖用不同分母；未核实的历史名单有效期保持 unknown。金融业不适用字段和有效零／负值不能作为统一缺失。

分类报告分层行业数、未知行业成员、上市日期缺值、来源缺行和本地保留行。页面继续标识当前成员回溯的历史局限。市值汇总继续只有冻结成员全部具备有效值才给完整总额。

## 4. 数据结构与兼容方案

推荐采用“历史引用保留＋新公共引用”的明确双来源模型，避免一次重写约 90 万条事实的出处。

1. 四张财务表保留旧 `batch_id/source_id` 名称与历史值，允许仅新记录为空；增加 `collection_batch_id/collection_source_id`。CHECK 强制一行恰有一组完整引用，公共组采用 `(collection_source_id,collection_batch_id)` 组合外键，旧组同样校验所属批次。不得用两套裸整数 ID 共用一列，也不为新事实伪造旧来源占位行。
2. 因现有旧外键为 NOT NULL，不能只 ADD COLUMN 完成迁移。隔离库演练重建表、复制所有原 ID／金额／版本／hash／时间／旧引用、恢复索引及 View。迁移可能涉及整表复制，但不迁移旧来源语义；测量临时磁盘、耗时与锁占用。版本号在实施时按最新 schema 分配。
3. 新公共采集停止写 `market_financial_batches/sources`，二表保留历史。新旧来源读取用统一 resolver；行业 View 增加明确公共引用列，原旧引用列仍表示旧体系，消费者不能混淆 ID。
4. `sw_memberships` 增加上市日期公共来源／批次成对引用，旧 listing_source_id 保持历史；新上市日期不同时双写 sw_listing_sources。新名单可沿用旧上市日期引用，但不能将其标为新采集。分类多来源关系在 sw_imports 清单、membership_checks 结果中保存公共 ID 与用途，无需新建映射表或逐行复制文件元数据。实现时验证引用指向真实同 operation 数据段，且来源与批次一致。
5. `_load` 缓存、报告期更新时间、来源展示和中断恢复合并读取旧成功发布与新公共成功发布，不能简单 max 两种表的 ID。缓存 token 可用两套各自的 max ID／计数／真实完成时间组成；无变化核对使用批次完成时间，事实出处时间保持不变。
6. 不新建公共 operation 表。sw_update_runs 现无 cancelled 状态：第一版可以任务 failed＋结果 termination=cancelled、子批次 cancelled，页面明确显示已取消；如需扩展表约束，列为实施决策而非暗改。

结构迁移与日常部署已在获授权的实施阶段完成；结果和体积核查分别记录于 `PROGRESS.md` 及 `data/verification/reports/`。

## 5. 旧入口处置

- 保留旧解析、只读核对、离线导入／压缩工具；不修改旧历史。
- 已停用 industry_bulk.collect 继续停用。
- 分类初始化／foundation-only 下载迁入同一新适配入口，沿用官方沪深名单；旧新浪范围若显式保留，必须标明 universe 定义并走公共 sina。不可静默换名单口径后复用旧幂等键。
- 旧试点混合下载、财务四字段／业绩旧表在线补采及旧市值在线工具，默认停止旧联网入口并给新命令指引，保留离线能力；如有必须继续的专项用途，应先接公共 collector 并单独确认写入范围。不能仅以“历史工具”名义继续绕过预算。
- 本次不迁移旧 market_price_* 来源，也不修改个股新浪财务优先级；涉及同源绕行的残留请求必须在验收报告明确列出，不能宣称整个仓库所有请求已统一。

## 6. 分阶段实施

| 阶段 | 产出 | 通过条件 |
| --- | --- | --- |
| P0 契约与离线预检 | 入口清单、字段／引用依赖清单、范围键、隔离库迁移容量估算 | 旧入口无遗漏；公共 get/invoke 的拒绝与恢复契约经审核 |
| P1 兼容结构与读取 | 四表引用扩展、统一来源 resolver、View／缓存／维护统计适配 | 迁移前后 ID、金额、版本、hash、时间和旧引用完全一致；新旧来源不串号 |
| P2 财务采集接入 | 日常与历史统一 collector、分段／来源／原子发布／恢复 | 不再新写旧财务元数据；失败也留来源；四表发布原子性成立 |
| P3 分类与上市日期接入 | 官方名单／申万文件调度归档、导入引用、分类初始化入口 | 与指数共享 sws 预算；名单复用、旧值保留可辨；季度冻结不受影响 |
| P4 旧在线入口收口 | 停用或适配清单、命令提示、文档 | 本计划覆盖的入口无裸网络／自建限速；离线工具继续可用 |
| P5 验收与部署 | 隔离测试、迁移校验报告、必要的有限真实样本、部署记录 | 财务／行业页面回归；核对运行身份后重启 8765 并保持可验收 |

P5 有限真实样本的请求范围在实施时明确；优先复用本地样本，不自动重抓 48 期。原库上线前正式备份 SQLite，并保全旧财务、行业与公共原文目录及 manifest／哈希；禁止以 SQLite 单文件备份声称完整备份。源码回退不能替代新结构／数据回退，恢复应选择已校验的迁移前完整备份，另保留迁移后新增资料。

## 7. 验收清单

- 隔离库＋假时钟＋模拟响应：跨任务／线程／进程共享预算、重启状态、403/429/反爬、Retry-After、冷却单探测、最多三次临时重试、取消等待、发送前预留预算；与既有指数／快照交叉测试。
- 原文先归档：失败响应、解析失败、网络无响应、每次重试均有记录；真实取得时间、原文／存储双哈希、路径安全和 reused_source_id 可验证。
- 财务：四表中途失败不发布本期，已完成其他报告期保留；分页截断／变动／重复／字段变化拒绝；源明确空与失败分开；有效零负值、真实修订、无变化保留出处、空值补全／疑似撤回、同源拒绝不进入旧重试。
- 恢复：四段部分完成、同日缓存、跨日不冒充新采集、范围／口径变化、损坏原文、未知覆盖、空结果后续再查均可验证。
- 分类：XLS/XLSX 假 HTML／损坏文件、沪深板块遗漏、父子层级、未知行业、上市日期缺失、保留旧成员／旧行业、名单未变、重复启动、当天失败显式重试；分类失败沿用旧名单提示，季度冻结不变。
- 来源读取：旧与公共 ID 恰好相同时不串号；迁移后 provenance、页面更新时间及缓存失效正确；无数据时间显示未记录。
- 维护页／统计 API：所有表用途、分类、记录数、大小及真实时间路径正确；空表／缺值／逻辑 View 不重复统计；外键无孤儿、公共来源与 batch 一致。
- 回归优先覆盖 test_market_financial、test_industry_memberships、test_industry_financial_reader、test_industry_memory、test_industry_cap_summary、test_index_collection、快照相关测试和行业页面。仅实施后运行，本阶段未运行测试。
- `docs/sqlite-storage.md`、`docs/database-maintenance.md`、`docs/industry-data-schema.md`、财务／快照来源文档、公共采集说明同步实际行为；指标本身若不变不改公式。新报告放 data/verification/reports/ 对应目录，常用审计工具放 tools/。

## 8. 后续单独决策

旧元数据归并可作为第二阶段：先只读核对文件可用性、双哈希可恢复程度、未知参数／时间、原始 HTTP 与重建 JSON 的差异，再提交明确映射与回滚方案。不能把旧归档时间标为新请求时间，不能为没有证据的旧失败补造来源。旧财务／市值表删除、进一步历史回填、自动定时均另行处理。

协作交接：Sol 已完成本计划与决策；获得实施授权后，Luna 先读本目录三个文件再执行并持续更新 PROGRESS；Sol 最后结合这三个文件与 Git diff 审查。不得混用其他计划状态。
