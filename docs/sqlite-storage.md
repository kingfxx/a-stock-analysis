# SQLite 存储、数据更新与备份

当前结构版本为v42。v36新增`market_indicator_observations`（单表市场拥挤度及收盘点位、两类亿元金额、来源通道和修订）和`collection_schedules`（每日检查配置），不建立指标序列表。观察表更新时间依据真实obtained_at/updated_at，配置依据updated_at，空表不以交易日代替。首次来源归档，后续拥挤度HTML更新经用户授权仅保存公共请求元数据及事实，不落响应/逐次报告文件。详见[大盘拥挤度](index-data-collection.md#大盘拥挤度采集与查询)。

v37仅扩展既有`index_price_observations`，新增volume、amount、change_points、change_pct、turnover_rate、amount_ratio、constituent_count、mean_price、float_market_cap和mean_float_market_cap十个可空字段，另增field_lineage_json字段级来源/估算JSON，不新增业务表。中证/申万原series及行情行就地升级v2，保留ID及OHLC，不保留v1行情副本；Legu/Sina继续v1。每个series_id/observation_date物理唯一，历史修订就地更新，version只作计数；不建立独立derived行情。估算字段标记及父依赖同行存储，官方值优先；仅补算保留原source/batch/obtained_at，computed_at在估算JSON中，updated_at是真实写入时间；缺依赖保持NULL或撤回。单位、字段映射、缺值和派生估算规则见[指数行情字段映射](index-data-collection.md#field-mapping)。v38后逻辑来源/时间语义保持，物理来源/时间格式见下节。

## v38 指数行情紧凑存储

`index_price_observations`仍为唯一物理行情表，不新增业务副本，26列重建为23列，保留原行/序列ID、14数值及NULL/零/负值、修订计数、来源、逻辑内容hash和完整溯源：

| 物理列/结构 | 存储及读取规则 |
|---|---|
| `observation_date` | 1970-01-01起整数日；查询参数先转换，读取返回原ISO日期，保留日期范围索引命中 |
| `content_hash` | 完整SHA256的32字节BLOB，外部读取恢复64字符hex，不截短哈希；原逻辑hash不变 |
| `updated_at` | 可精确恢复的规范UTC ISO时间存整数微秒，其他历史ISO形式保留原字符串；维护统计分类型取真实最大值后解码 |
| `source_id` | FK关联`collection_sources.id`；逻辑`batch_id/obtained_at`从该来源读取，发布仍校验来源/批次/提供方/真实时间，不重复逐行存储 |
| 最新行 | `(series_id,observation_date)`物理唯一，逻辑`is_latest`恒为1；历史修订不追加副本 |
| `field_lineage_json` | 固定格式2的位置数组，字段位图顺序固定；原始修订/字段来源差异、计算时来源/输入/修订/分母事实ID/version/计算时间均保留。空证据为`[]`，异常/撤回/未知成员显式`{"fallback":完整原证据}`，不丢失未识别信息 |
| 索引 | 仅序列×日期唯一、日期×序列检索两项；移除被物理唯一覆盖的版本唯一和latest部分索引 |

统一编解码在`stock_analysis/domains/indices/index_price_codec.py`。源批次/取得时间通过来源ID恢复，估算父分母事实保留原修订行，不能就地修改已引用历史。格式2的公式/口径及14字段序号固定，改动必须升级协议/定义，不能修改常量使旧证据产生新解释。完整展开时批量加载父引用并缓存，日常图表仅读日期/点位；未变化估算直接检查紧凑依赖快照，不展开整套证据。

新增只读View `index_price_observations_read`恢复ISO日期、hex hash、来源批次/取得时间和最新标记；其中`field_lineage_json`仍为内部紧凑结构，完整说明通过codec恢复。View不占业务存储，维护分类“指数数据”。物理表和View的维护时间依据均为解码后的真实updated_at及关联来源obtained_at，空表显示无数据，不用观察日替代。

v38迁移逐行校验完整逻辑还原后原子切换，恢复缓存失效触发器；失败回滚可修复后重试。schema事务不联网、不自动回放，已有v37迁移文件保持不变。物理释放空间需在正式备份和实例锁保护下执行VACUUM；来源原文仍另行保留。性能和发布验收见当前[实施计划](plans/20261018_行情紧凑存储落地与性能验收/PLAN.md)。


2026-10-17用户授权20日离线拥挤度补值，复用同表新增独立身份`ashares_congestion/all_a/bigquant/bigquant_calculated/daily/amount_rank_top5_v1`，不改schema。value为自算比例，close来自现有中证上证同日点数，两类金额为亿元；原Legu缺值观察保留。source是明确标记LOCAL_DERIVATION的计算证据，原行情及点位事实引用在证据中追溯；obtained_at为该计算证据生成时间，updated_at为发布时刻。详见[补值说明](data-sources/market-congestion-gap-backfill.md)。

v35 对 `financial_reports.raw_json` 逐条 zlib 6 无损压缩，历史行在 schema 事务中校验原内容哈希后迁移，其他列、索引及来源关系不变。SQLite 原 TEXT 列兼容存储自描述 BLOB，不新增业务表或科目字典。应用仓储和直接读取入口通过 `financial_storage` 显式恢复原 JSON 文本；独立研报技能支持相同编码。详见 [压缩协议与迁移](financial-json-storage.md)。

v34 新增个股主营构成 `business_segment_versions` 与 `business_segment_items`；当前结构版本以v35及维护页为准。首次／full／180天审计获取上海当天前十个日历年内实际报告期，增量重叠最近两期。复用公共来源和个股同步状态，HTTP原始JSON不落盘，仅保存结构化业务版本；详见[个股主营构成](stock-collection.md#个股主营构成)。

v31继续只更新 `market_cap_latest`，新增可读取的同表独立停牌估值系列 `historical-suspended-preclose-v1`：东财有效值优先，其次经验证的停牌参考价系列、普通历史合成、旧同日市值。原空价记录保留；实际价格日集中登记于发布batch的 `result_json.price_dates`，快照交易日仍为季末，真实取得／更新时间规则不变。详见[停牌参考价补齐](data-sources/market-suspended-price.md)。

v30仅更新 `market_cap_latest`，不新增表：按字段优先读取东财有效值、独立 `historical-composite-v1` 同日合成值、旧同日市值。来源使用公共批次／来源表，时间依据仍为真实 `obtained_at/updated_at`，逻辑View显示「未记录」。详见[历史合成快照](data-sources/market-trade-snapshot-composite.md)。

v29新增业务表 `market_trade_snapshot`：全市场未复权股价、涨跌幅、股本、市值、估值等22个源字段，当前只采季度末，事实按实际交易日识别以兼容以后日频。复用公共 `collection_batches/sources/provider_state` 和现有 `sw_update_runs`，不新增采集控制表。`market_cap_latest` 为新快照优先、旧同日市值回退的逻辑View。新表为行业数据，时间依据为真实 `obtained_at/updated_at`；空表或缺时间显示未记录，交易日不代替更新时间。公共来源表分类为运行与配置。原文归档在公共 `collection_sources/`，SQLite备份不包含来源文件。详见[全市场交易日快照](data-sources/market-trade-snapshot.md)。

指数采集新增 v23/v24：公共 `collection_batches/sources/provider_state`，稳定指数身份、历史目录、提供方映射、指标定义、统一估值事实及操作状态。未迁移旧财务或价格记录；用途与真实时间依据已登记维护页。表关系、来源目录完整备份及采集保护见[指数估值采集说明](index-data-collection.md)。

本文依据当前代码整理；取消关注、恢复和个股清理部分更新于 2026-10-07，全行业结构索引更新于 2026-10-10，指数点位说明更新于 2026-10-18，交易日快照更新于 2026-10-14，主营构成更新于2026-10-16，大盘拥挤度更新于2026-10-17，当前数据库结构版本为 **37**，表数量以维护页实时统计为准。历史验收中的文件大小、行数和耗时属于当时的测量，不代表当前数据库状态，详见文末专项记录。

## 1. 文件位置与启动过程

| 路径 | 用途 | 是否自动清理 |
| --- | --- | --- |
| `data/stock_analysis.sqlite3` | 日常数据库，保存原始事实、同步状态和价格版本 | 不自动删除 |
| `data/stock_analysis.sqlite3.lock` | 数据库实例锁文件 | 文件可长期保留，进程退出会释放操作系统锁 |
| `data/backups/` | 正式日备份、升级前备份及手动备份 | 只有规定命名的日备份参与轮转 |
| `data/fundamentals/`、`data/valuation/`、`data/chips/` | 旧 JSON 缓存及部分导入归档 | 保留原文件；正常运行以 SQLite 为准 |
| `data/collection_sources/` | 公共指数及交易日快照逐次请求的 gzip 原文；双哈希登记在公共来源表，SQLite 备份不包含这些文件 | 正式来源证据保留；迁移须同时复制并验哈希 |
| `data/verification/` | 来源样本、报告、核对资料、维护工具和后台日志 | 按项目记忆整理、清理过期测试产物 |

`data/verification/` 的分类为 `samples/`、`reports/`、`audits/`、`tools/`、`logs/`。正式库与正式备份不属于测试清理范围；数据库、备份和本地验证资料不进入 Git。

启动 `python app.py --port 8765` 时，`server.serve()` 按以下顺序执行：

1. 取得数据库实例锁，再绑定 HTTP 端口。
2. 初始化或升级结构，并执行完整性及业务关系检查。
3. 检查或生成当天日备份，然后将遗留的 `running` 同步任务标记为失败，清理相应的候选价格版本。
4. 尝试导入旧筹码、财报、分红和估值 JSON；单个文件导入失败会记录原因并保留原文件。
5. 开始处理页面和 API 请求。

启动失败时关闭已经绑定的端口并释放实例锁。**同一个数据库只允许一个应用或维护实例持有实例锁，即使端口不同也不能同时启动。** `.lock` 文件存在不等于数据库仍被占用，不应通过删除它绕过锁。

8765 是日常运行端口，不用于自动测试或临时预览服务。测试服务显式使用其他空闲端口和隔离数据库；代码修改通过验证后，需要加载新代码时自动重启 8765 原数据库后台，供用户人工验收。运行规则以当前项目指令为准。

## 2. 存储表说明

全行业数据的完整表结构、字段含义、接口字段映射、单位、主外键、来源追溯及新旧表边界见 [全行业数据表结构与字段字典](industry-data-schema.md)（四张财务表、行业分类/成员、市值、批次/来源及读取 View）。

### 2.1 个股与共享行情事实

本节列出个股及共享行情事实；全行业财务、分类、成员和市值表统一见 [2.5 全行业数据表清单](#25-全行业数据表清单)。`industry_snapshots` 是个股关联的行业估值快照，保留在本节。

下表中的“唯一身份”是主键或唯一约束；`instrument_id` 关联 `instruments.id`，多数来源记录通过 `run_id` 关联产生它的同步任务。

| 表 | 保存内容与主要字段 | 唯一身份及使用方式 |
| --- | --- | --- |
| `instruments` | 股票交易所、六位代码、名称和创建时间 | `id` 为主键；`(exchange, code)` 唯一。代码是文本，保留前导零 |
| `business_segment_versions` | 主营构成股票、来源、报告期／报告名、递增修订号、前版、当前标志、结构化业务哈希、定义版本、明细数、run／batch及实际取得时间 | 股票×来源×报告期×修订号唯一；部分唯一索引确保每期仅一个当前版本。哈希不含排序及取得时间，允许A→B→A；不保存原始JSON |
| `business_segment_items` | 所属版本、行业／产品／地区、原始项目代码／名称、收入／成本／毛利及四种比例、展示排序、source／batch | 版本×维度×项目键唯一；项目键为代码与名称的JSON编码，不跨期归类。金额及比例为可空十进制TEXT；source与batch复合外键，触发器核验父版本批次。时间关联父版本obtained_at |
| `financial_reports` | 逐条无损压缩的财报原始 JSON、报告类型、报告期、披露日期、来源更新时间、内容哈希与取得时间 | `(instrument_id, source, report_type, period)`；新浪类型为 `lrb` 利润表、`fzb` 资产负债表、`llb` 现金流量表、`gjzb` 关键指标，旧合并记录以 `legacy/merged` 保留 |
| `stock_announcement_identities` | 巨潮提供方组织标识、身份状态、来源/批次及实际查询和更新时间 | 股票 `instrument_id` 唯一；身份来源通过公共 `collection_sources` 与同一批次外键追溯 |
| `stock_announcement_metadata` | 公告编号及标题、公告时间、附件路径、报告期候选、类型/修订标记、字段指纹和版本来源 | 股票×公告编号×版本唯一；`periods_json` 保留一条多年度说明涉及的多个期间；仅结构化元数据 |
| `report_disclosure_dates` | 报告期首次披露日、最新已知修订日、规则/结果版本、候选依据及状态 | 股票×报告期单一当前结果，旧结果保留版本链；人工覆盖仍由 `report_overrides` 最后应用 |
| `dividend_events` | 分红事件标识、报告期、预案/公告/登记/除权日期、状态、每十股现金分红和原始 JSON | `id` 为主键；`(instrument_id, source, event_key)` 唯一。业务读取仅让已经实施且除权日不晚于今天的现金事件参与计算 |
| `valuation_observations` | 指标、实际观察日期、数值、来源窗口、采样口径版本和原始 JSON | `(instrument_id, source, metric, observed_on)`；当前直接存储 `pe`、`pb`、`market_cap`，保留同一日期曾来自哪些来源窗口 |
| `industry_snapshots` | 行业名称/代码、分类口径、快照时间和行业估值原始 JSON | `id` 为主键；`(instrument_id, source, snapshot_at)` 唯一。来源内容变化才增加快照，页面读取最新一份 |
| `financing_daily` | 交易日、融资余额、融券余额、融资融券余额、融资净买入、来源收盘价、原始 JSON 和沿用字段 | `(instrument_id, source, trade_date)`；保存完整日度历史，前端取最近五年用于 1/3/5 年筛选。净买入允许为负 |
| `shareholder_observations` | 来源记录键、统计截止日、公告日、股东人数及口径、原始 JSON | `(instrument_id, source, source_record_key)`；`holder_scope` 分为 `total`、`a_share`、`unknown`，不同来源和人数口径分别保留 |
| `raw_daily_prices` | 腾讯不复权日行情的开、高、低、收、成交量、原始 JSON、哈希和取得时间 | `(instrument_id, source, trade_date)`；为股息率计算及不复权价格展示提供共享行情 |
| `adjusted_price_versions` | 前复权历史版本的股票、来源、覆盖范围、行数、验证时间、来源口径及 `candidate/complete` 状态 | `id` 为主键；保存一代完整价格历史的元数据，由 `sync_state` 指向活动版本 |
| `adjusted_daily_prices` | 某个前复权版本中的交易日期、收盘价和原始 JSON | `(version_id, trade_date)`；关联 `adjusted_price_versions.id`，删除版本时级联删除该代日价 |

`raw_daily_prices.close` 必须大于零；前复权日价表允许零值和负值。缺失数据使用空值或断点，不能用零值代替。

结构 22 扩展 `market_financial_industry` 的同报告期 `bps`，供行业公司 PB 计算；不新增财务副本，来源及更新时间规则不变。

结构 21 新增 `market_quarterly_prices`、`market_price_sources`、`market_price_batches`，保存全市场季末稀疏未复权价格及共享来源／批次，不绑定 `instruments`。见[全市场季度末未复权收盘价](data-sources/market-quarterly-prices.md)。

### 2.2 同步、兼容与维护元数据

| 表 | 保存内容与主要字段 | 作用及关联 |
| --- | --- | --- |
| `sync_runs` | 每次同步的股票、数据集、来源、复权口径、解析器/计算口径版本、触发原因、请求范围、状态、记录数及错误 | `id` 为主键；事实表的 `run_id` 用它追溯来源。状态为 `running`、`success`、`no_data`、`failed`、`superseded` |
| `sync_state` | 当前修订号、数据状态、成功覆盖范围、水位、检查/成功时间、下次全量核查时间、最后成功任务及活动价格版本 | `(instrument_id, dataset, source, adjustment)`；用于缓存判断、增量起点和并发提交校验 |
| `price_version_leases` | 价格版本及租约到期时间 | `version_id` 为主键；固定读取版本时续租 5 分钟，避免正在使用的旧价格代被清理 |
| `legacy_imports` | 旧文件路径、内容哈希、数据集、股票、导入任务、记录数和导入时间 | `(path, dataset)`；防止同一旧文件的同一数据集重复导入，同一文件可分别导入财报与分红等数据集 |
| `report_overrides` | 报告期、字段名、JSON 值、`manual/legacy` 来源、原路径和更新时间 | `(instrument_id, period, field_name)`；覆盖业务读取结果，支持显式 `0` 和 `null`。手动项不关联任务，旧缓存导入项关联任务 |
| `legacy_valuation_snapshots` | 旧缓存的观察月份、行 JSON、原路径、原计算口径和导入时间 | `(instrument_id, source_path, observed_month)`；兼容旧月度结果，不把它冒充新来源的日度观察 |
| `schema_migrations` | 已执行迁移的版本、文件名、内容校验和及应用时间 | `version` 为主键；与 SQLite 的 `user_version` 和应用预期迁移列表共同校验结构 |

版本 5 另有四张用户整理表：`stock_groups` 保存自建组名称和顺序，`stock_group_members` 保存分组与股票的多对多归属，`stock_recent_views` 保存最后查看时间，`stock_picker_preferences` 保存上次选中的组。删除自建组级联删除其归属，不删除 `instruments` 或任何来源事实。最近查看、全部股票和未分组由查询生成，不作为自建组存储；最近查看按时间取最多 20 只。拼音索引读取时生成，不写入来源事实。

版本 6 在 `stock_group_members` 增加 `position`，保存每只股票在对应自建组中的顺序，按 `position` 从小到大读取。同一只股票在不同组中可有不同位置；新成员追加在组末尾，重复加入不改变原位置。升级时按此前的代码顺序初始化，保留所有分组和归属。

版本 15 新增 `stock_unfollowed` 和 `stock_unfollowed_groups`，将取消关注状态及原分组与来源事实分开保存。已有股票升级后仍全部关注。取消关注清除当前分组归属及最近查看，选股列表排除取消关注股票；保留所有来源事实和研究文件。后台维护可批量恢复至仍存在的原分组，删除原组会级联清除其恢复归属，避免组编号复用误恢复。两张新表的维护更新时间均来自 `unfollowed_at`（直接读取／关联读取）。详情见[后台维护页](database-maintenance.md#已取消关注)。

版本 16 新增 `stock_cleanup_runs` 作为未关注个股清理的审计与事务提交标记，使用 `created_at`／`completed_at` 记录写入时间。清理只操作固定个股白名单，不修改 `sw_*` 行业表或行业来源文件；检查保留研究、文件共享和价格租约；明确选择财务类别时清理所选股票全部 `financial_reports` 原始记录，行业事实和研究快照保留。文件先暂存，数据库删除与提交标记原子提交；启动在旧缓存导入前完成中断恢复。清理前不创建备份，数据库空间整理需在维护页单独确认执行。详细范围、预览及空间口径见[未关注股票数据清理](database-maintenance.md#未关注股票数据清理)。

版本 7 增加三张 AI 研判表：`ai_analysis_snapshots` 保存不可修改的分析输入与来源快照，`ai_analysis_runs` 追加保存任务状态和成功报告，`ai_analysis_preferences` 保存默认模型等非敏感设置。凭据仅在 Windows DPAPI 保护的忽略目录保存，不随数据库备份恢复。AI 分析不单独触发备份，沿用第 5 节的日备份规则；启动将 AI 活动任务记为中断而不自动重新调用模型。详见 [AI 使用与凭据说明](ai-stock-assessment.md)。

`sqlite_schema` 是 SQLite 自身的结构目录，表空间统计可能列出它，但它不计入应用表数量（以维护页实时统计为准）。`legacy_imports_new` 和 `financing_daily_compact` 是迁移中的临时表，成功迁移后不会保留为额外业务表。

### 2.3 原始事实与计算结果

数据库保存来源事实和必要的追溯信息；财务图表结果在读取时计算，季度行业市值总额使用快照发布后持久化的汇总版本：

- 财务季度、年度和 TTM 视图由财报事实重建，没有分别保存三套财报结果。读取时先使用当前来源记录，以旧合并记录补充缺失项，最后应用 `report_overrides`。
- PS 使用市值观察及当时已披露的 TTM 营收计算；股息率使用已实施分红事件和共享不复权价格计算，不作为独立来源指标重复存入 `valuation_observations`。
- 日、周、月展示不分别保存三份行情。融资五年视图按周聚合：余额和股价取当周最后一条记录，净买入为所选范围内当周日度值之和；有缺失净买入时周合计留空。
- `raw_json` 是程序保存的来源记录 JSON，不保证保留整份 HTTP 响应的原始字节。`content_hash` 用于识别所保存内容；完整请求样本另放在 `data/verification/samples/`。

### 2.4 融资字段沿用与版本 4 精简

`financing_daily` 不再同时保存完整原始 JSON、完整合并 JSON 和全量字段来源三份重复对象。当前存储为：

- `raw_json`：本次来源返回的记录。
- 数值列：供查询和绘图使用的融资余额等数值。
- `retained_fields_json`：仅保存本次来源缺失、但历史已存在的字段和原任务编号；通常为 `{}`。

例如，来源本次没有返回某个历史字段时，沿用项形如：

```json
{"RZRQYE":{"value":123456.0,"run_id":42}}
```

业务有效记录由沿用项与当前原始记录合并。连续缺失继续保留最初的来源；字段重新出现时删除沿用项，明确返回 `null` 也属于重新出现。完整性检查会验证沿用任务属于同股票、同来源、成功的融资同步。


### 2.5 全行业数据表清单

行业专用数据包括财务四表、交易日快照、旧价格与市值历史表、成员及任务表，并复用跨领域公共采集三表。表数量以维护页实时发现为准。`sw_` 前缀不代表全部停用；旧财务表已退出行业日常采集和读取，旧市值/价格表保留供历史追溯和旧工具使用，行业页面已改为直接读取交易日快照。

Schema v32 为四张财务表增加可空的 `collection_batch_id` / `collection_source_id`。每行必须且只能使用一组完整来源引用：历史行继续使用旧 `batch_id` / `source_id`，新公共来源行使用公共列，组合外键确保来源与批次相配。迁移重建财务表并逐列复制旧行，不更改旧 ID 或内容。`market_financial_industry` 暴露旧引用和公共引用两组独立列；读取方不得跨命名空间解释整数 ID。`sw_memberships` 的公共上市日期来源列使用 `collection_sources` 记录，其采集时间由维护页沿关联来源读取。

Schema v33 新增三张巨潮公告日期元数据表。`metadata_only` 请求不写入原文文件/哈希或响应 BLOB；来源字段保留公共批次/来源 ID，触发器校验提供方、批次、数据集及股票 run 身份。实际请求完成范围不是巨潮历史完整性声明。

| 分类 | 表 / View | 保存内容与来源 | 当前用途 |
| --- | --- | --- | --- |
| 财务 | `market_financial_income` | 利润表摘要全字段，东财 `RPT_DMSK_FN_INCOME` | 当前全市场财务采集；行业营收、利润和毛利率计算来源 |
| 财务 | `market_financial_balance` | 资产负债表摘要全字段，东财 `RPT_DMSK_FN_BALANCE` | 当前全市场财务采集与历史保存 |
| 财务 | `market_financial_cashflow` | 现金流量表摘要全字段，东财 `RPT_DMSK_FN_CASHFLOW` | 当前全市场财务采集与历史保存 |
| 财务 | `market_financial_performance` | 业绩指标全字段，东财 `RPT_LICO_FN_CPD` | 当前全市场财务采集；行业内公司对比的加权 ROE 来源 |
| 财务采集 | `market_financial_batches` | 迁移前每报告期四表批次与来源元数据 | 历史财务行通过旧 `batch_id` 追溯 |
| 财务来源 | `market_financial_sources` | 迁移前分页参数、gzip 响应路径、哈希、大小及字段列表 | 历史财务行通过旧 `source_id` 追溯 |
| 读取 View | `market_financial_industry` | 最新利润表左连接同公司同报告期最新业绩指标 | 行业页面统一取数入口；不存储副本，计算在 Python 完成 |
| 交易日快照 | `market_trade_snapshot` | 东财 `RPT_VALUEANALYSIS_DET` 全部22个已登记源字段；当前只采季末 | 当前季末未复权股价、股本、市值及估值来源；身份按实际交易日兼容日频 |
| 市值读取 View | `market_cap_latest` | 新快照有效市值优先、旧表同日最新有效市值回退 | 兼容旧工具读取；行业页面直接查询 `market_trade_snapshot` |
| 旧季度价格 | `market_quarterly_prices` / `market_price_sources` / `market_price_batches` | BigQuant 季末未复权收盘价及历史来源/批次；不是总市值 | 行业页面已不读取或回退，日常快照按钮停写且不调BigQuant；保留历史、合成输入追溯、采集日期映射核对及旧工具用途，见[来源说明](data-sources/market-quarterly-prices.md) |
| 公共采集 | `collection_batches` / `collection_sources` / `collection_provider_state` | 分段与覆盖、逐次请求结果及同源共享预算/冷却；指数、交易快照及市场财务/行业保存完整归档来源，个股按已授权例外仅保存 metadata-only 请求元数据 | 覆盖指数、交易快照、市场财务/行业及个股；个股来源无原文文件、哈希或重放能力，见[个股采集说明](stock-collection.md) |
| 导入版本 | `sw_imports` | 名单/旧市值及快照汇总审计清单、哈希、真实时间及成员版本指针 | 当前分类及市值审计；独立于财务批次 |
| 行业分类 | `sw_industries` | 申万 SW2021 一至三级分类代码、名称及父子关系 | 当前行业层级与分类查询 |
| 行业成员 | `sw_memberships` | 各版本沪深公司名单、申万归属、上市日期及来源引用 | 当前行业成员筛选与公司名称/上市日期展示 |
| 行业成员 | `sw_membership_history` | 来源提供的行业归属生效及变更记录 | 保存分类变更依据；不等同于完整历史全市场名单 |
| 上市日期来源 | `sw_listing_sources` | 沪深交易所名单中上市日期的共享证据及采集时间 | 当前上市日期来源追溯 |
| 名单检查 | `sw_membership_checks` | 北京时间每日名单检查状态、时间与结果 | 当前日常采集前名单检查及去重 |
| 公司市值 | `sw_cap_facts` | 旧公司交易日总市值；历史批量来源主要为东财 `RPT_VALUEANALYSIS_DET`，旧来源可含百度/腾讯；2016—2018 一次性缺口工具可写入历史未复权价格 × 历史总股本的计算值，详见[来源说明](data-sources/historical-market-cap-backfill.md) | 行业页面已不读取或回退；保留旧历史、合成输入追溯及 `market_cap_latest` 的旧工具兼容读取；新日常采集停写 |
| 市值来源 | `sw_cap_provenance` | 共享市值来源参数、字段、单位换算和文件证据 | 行业页面不直接读取；旧市值事实通过 `provenance_id` 解析来源，保留历史核验及合成输入证据；新快照使用公共来源 |
| 季度市值成员 | `sw_cap_quarter_rosters` | 每季度冻结名单的目标日期、构成口径及成员版本 | 当前季度市值成员口径控制 |
| 季度市值成员 | `sw_cap_quarter_members` | 冻结季度内逐公司行业归属 | 当前季度市值逐公司成员查询 |
| 行业市值 | `sw_industry_cap_quarters` | 三级行业季度市值、应有/已知公司数及成员口径 | 快照发布后按冻结季度名单生成审计版本；旧导入行保留 |
| 任务管理 | `sw_update_runs` | 日常更新与历史补采的动作、目标、状态、时间及错误 | 当前四表采集、市值采集等任务进展与互斥 |
| 旧财务 | `sw_financial_facts` | 旧版轻量财务字段、报告期、导入版本及来源引用 | 仅保留旧历史与维护工具；日常按钮停写，行业页面不读取 |
| 旧财务来源 | `sw_financial_provenance` | 旧财务字段的共享来源与指标定义 | 仅供旧财务历史追溯及维护工具 |

四张财务表按证券、交易所、报告期保存最新记录及真实修订版本；利润表、现金流量表为累计流量，资产负债表为期末余额。四表采集包括沪深北 A 股，行业页面仍按沪深申万成员筛选。详细字段、主外键、单位、计算口径和 SQL 示例见 [全行业数据表结构与字段字典](industry-data-schema.md)。

#### 旧市值三表的页面与保留关系（2026-10-14核查）

行业公司行市值与未复权股价读取 `market_trade_snapshot`；季度总额、覆盖率和排行总额按季度读取 `sw_industry_cap_quarters`：已有新来源审计版本时优先使用，尚未生成新版本的历史季度沿用原有有效汇总。快照汇总依据 `sw_cap_quarter_rosters` 的精确目标交易日和 `sw_cap_quarter_members` 冻结名单生成，并在现有 `sw_imports` 保存审计清单、在 `sw_update_runs.result_json.aggregation_results` 保存新来源季度版本映射；不新增控制表或迁移，也不批量回填既有历史期。新快照批次及汇总版本使行业缓存失效。

三张旧表仍在数据库维护页显示记录数、大小、用途及真实时间，但不再提供日常行业页面的展示金额。旧导入代码仍可写旧事实、来源及行业汇总，不能将日常按钮停写理解为所有历史工具已移除。

`market_cap_latest` View仍保留 `sw_cap_facts` 的同日回退，供旧工具兼容读取；当前行业页面不依赖该View。2026-10-14核查时，View中实际命中 `legacy:sw_cap_facts` 的记录为0，此数是该次数据状态，不是永久保证。当前旧行业汇总18,359条，覆盖2016Q1—2026Q3，最近关联写入批次135的取得时间为2026-10-09 21:40:34（北京时间）；这些统计不是页面最新市值的水位。

旧市值及 `sw_cap_provenance` 仍被历史合成证据引用，原事实、修订、批次、来源参数和文件证据共同用于复算及核验。页面不读取旧金额不等于可以删除旧表；本次仅登记用途，不清理数据。后续若计划归档或移除，须另行盘点旧工具、View及合成证据依赖，并保留可复核的来源关系。来源文件不包含在SQLite备份中。

### 2.6 指数估值整数序列（v25）

`index_valuation_series` 保存指数、提供方、口径、频率及定义版本，事实仅存整数series_id。两套整数唯一索引分别保护历史版本与最新观察，旧index_valuation_dates已移除。API还原原有身份字段，来源/批次组合外键仍保留。迁移自动备份，并在事务内核对全部旧值；序列created_at是真实建档时间，历史事实obtained_at/updated_at不改变。详见[指数数据说明](index-data-collection.md)。

### 2.7 指数估值公共采集与更新

指数估值通过以下关系独立存储，不与个股估值事实或旧行业财务表混写：

| 表 | 存储职责 |
|---|---|
| `index_identities / index_catalog / index_provider_codes` | 稳定指数身份、类别与行业体系／层级、历史目录版本、提供方代码映射；未核实有效期保持未知。 |
| `index_metric_definitions / index_valuation_series` | 来源字段与估值口径定义，以及指数×提供方×序列×频率×定义版本的整数身份。 |
| `index_valuation_observations` | 日／月频估值事实和修订；通过series_id关联序列，通过source_id／batch_id组合关联来源与批次。 |
| `collection_batches / collection_sources` | 分段执行与独立覆盖状态、逐次请求及原始响应追溯；原文在数据库旁collection_sources目录，不嵌入事实行。 |
| `collection_provider_state` | 按来源共享的请求预算和持久化冷却，跨任务／重启延续。 |
| `index_collection_operations` | 用户启动的一次任务、取消、进度与结果；一个任务可含多个分段和请求。 |

常规“更新近期估值”先查本地各来源／序列水位，仅补尾部新增日期；已最新的序列跳过估值请求。申万一级／二级独立检查成功分段，不能只用全表最大日期判定。月频移动观察点按真实日期保存。单独“核对近期修订”重抓日频10交易日／乐咕4个月，另有历史月份轮换核对。无变化不新增事实版本，真实修订留档；跳过请求仍记录任务状态。

2026-10-15授权的指数提速复用上述表，不新增结构迁移。三个提供方并行，同源最多2个在途请求；完成后等待2—5秒，滚动60秒30次，每完成20次额外休息10秒。`collection_provider_state.budget_json`继续保存共享发送记录与累计数，并登记完成计数、完成时间及在途请求／尝试号预留；真实占槽由数据库旁的提供方OS文件锁保证，进程退出释放锁，重启不清空预算或冷却。`index_collection_operations.result_json.providers`保存各来源当前进度，失败和取消来源也计入最终请求统计。其他采集继续执行各自原策略，详情见[指数采集说明](index-data-collection.md)。

指数进度面板的结构化计数另存于同一 `result_json.work_progress`：包含工作项账本、总体与各来源的成功／跳过／失败／已停止／待执行／进行中数量及当前说明。分母在范围确认后、估值请求前由本地范围预登记；并发状态更新使用短事务，结束后保留统计。该JSON为任务进度，不替代事实、来源或数据覆盖记录；旧操作没有此字段时显示未记录，不补造历史总数。此次仅扩展现有JSON内容，无表结构迁移。

日期已最新不代表历史完整或数值未修订，内部缺口与逐指标缺值需独立核验。公共结构目前用于指数和全市场交易日快照；旧财务／价格历史尚未统一迁移。SQLite备份不包含原始响应，完整追溯恢复须同步复制来源目录并校验双哈希。

来源能力、口径、请求保护、更新／修订核对、取消恢复与接口限制的detail文档见[指数估值采集与查询说明](index-data-collection.md)。

### 2.8 指数读取版本（v26）

`index_read_revisions` 保存单行 `scope='indices'`、非负整数 `revision` 及真实写入时间 `updated_at`。v26的15个触发器覆盖估值事实、序列、目录、指标定义及 `sw_industries`；v28再为点位定义、序列、事实的新增／修改／删除各增加触发器。相关变更在同一事务递增版本，用于目录和图表内存缓存失效。其他页面普通写入不触发；回滚不改变已提交版本。迁移不修改业务事实，初始化先备份；维护用途及更新时间依据已登记。

### 2.9 指数点位与归档复用（v27）

宽基、行业、策略及后续主题指数共用以下点位表，与估值事实分开存储，复用指数身份、目录、提供方映射及公共采集来源：

| 表 | 保存内容与主要字段 | 唯一身份及使用方式 |
| --- | --- | --- |
| `index_price_definitions` | 来源字段、点数单位、频率与缺值规则的版本定义 | `(provider, series_key, definition_version)`；`updated_at` 为实际登记时间 |
| `index_price_series` | 指数、提供方、接口序列、频率、收益口径及定义版本 | `id` 为主键，六字段联合唯一；事实引用整数 `series_id`，`created_at` 为实际建档时间 |
| `index_price_observations` | 实际观察日期、收盘点位及来源提供的开高低点、修订、批次与来源 | v37起`(series_id, observation_date)`物理唯一，`version`仅为同行修订计数；v38以source_id外键关联来源，batch_id/obtained_at由来源恢复，updated_at紧凑存储并解码为真实写入时间 |

中证历史响应提供日频开高低收，申万一级／二级分析响应提供日频收盘点位，乐咕 PE/PB 响应提供月频收盘点位。未来采集复用同一估值响应同步写入点位，不增加行情请求；已有成功归档通过显式启动的离线回放导入，保留原来源时间及复用关系，不重新采集。迁移本身不自动回放。

不同提供方、接口、日／月频、收益口径和定义版本分别保存，不拼接。有效点数须为有限正数，缺值留空，不由收盘点位补造开高低点；重复数据不改原出处，真实变化从v37起更新当前行；追溯复用公共来源归档和正式备份，不追加行情历史副本。点位查询为本地读取，指数页面本地叠加行情；采集成功不等于历史覆盖完整。

字段与收益口径、来源能力、回放／增量写入、读取及验收的详细说明见 [指数点位存储与归档复用](index-data-collection.md#指数点位存储与归档复用)。

### 2.10 全市场交易日快照（v29）

只新增一张业务表 `market_trade_snapshot` 和一个逻辑 View `market_cap_latest`。任务复用 `sw_update_runs`，逐日分段、逐次请求和预算复用公共三表；没有新增快照专用批次、来源或控制表。源字段转小写，共22列，加11个本地身份/版本/追溯字段，共33列。

- 身份和修订：`security_code + exchange + trade_date + definition_version + version`；最新部分唯一索引保证同公司/交易日/定义只有一个最新版本。季度属于采样策略，未来日频同日不会重复建档。
- 价格/股本/市值：`close_price` 为未复权收盘价，`total_shares/free_shares_a` 为股数INTEGER，`total_market_cap/notlimited_marketcap_a` 为金额REAL；源PE/PB/PCF/PS/PEG按原值保存，待核实辅助定义不替换页面计算。
- 来源：`source_id/batch_id` 组合外键引用 `collection_sources(id,batch_id)`；原文、请求参数及双哈希集中保存，行内不复制整行JSON或文件路径。
- 真实时间：`obtained_at` 为来源取得时间，`updated_at` 为本地写入时间。维护分类为行业数据，空表/缺时间显示未记录。View不保存时间，维护页显示未记录，实际时间查底层来源。
- 发布：按固定交易日全市场、500行分页，所有页完整校验后短事务提交。共用同源并发1、完成后10—15秒、60秒最多5次及每20次额外60—120秒休息；拒绝立即停止并持久冷却至少一小时。旧财务请求未全部接入，不能宣称所有东财旧流程已共用保护。
- 行业页面读取：公司市值与未复权股价来自 `market_trade_snapshot`；季度行业总额、覆盖率和排行总额来自快照生成并保存于 `sw_industry_cap_quarters` 的当前审计版本。公司市值按东财原始、已核验停牌估值、历史合成的顺序择优；旧 `sw_cap_facts` 和 `market_quarterly_prices` 不作为页面回退来源。PE仍按公司市值/TTM归母利润，PB仍按未复权价格/同报告期BPS。旧表/文件保留，新日常快照不再写旧市值或单独补BigQuant价格。

2026-10-14完成2015Q1—2026Q3共47期核验。实际有数据为2018Q1—2026Q3的35期、158,632条；2015—2017的12期来源明确为空，覆盖为partial，尚无法从此接口取得2016同比所需2015基期。历史名单有效期未核实、个别指标空值另记，执行complete不代表覆盖完整。

本次新表含索引88,465,408字节（约84.4MiB），公共响应归档20,555,653字节（约19.6MiB，包含中断复用副本）；这是本次实测，不是永久容量上限。47期原文双哈希/22字段回放、SQLite完整性及外键校验通过，旧市值/来源/价格逐行哈希与采集前备份一致。

字段、恢复及运行说明见[全市场交易日快照](data-sources/market-trade-snapshot.md)；[完整验收](../data/verification/reports/market-trade-snapshot-history-20261013.json)、[旧表完整性复核](../data/verification/reports/market-trade-snapshot-legacy-integrity-20261014.json)。

### 2.11 中美国债收益率（v39）

`sovereign_yield_observations`是中美国债数据的唯一业务事实表，归类为「宏观数据」，共21列；每行保存一个观察日、一个定义版本的一次完整数值修订。实际来源为东方财富`RPTA_WEB_TREASURYYIELD`，采集dataset为`bond_zh_us_rate`。来源原文、请求参数、批次和初始化清单复用公共采集表，不另建国债来源表或任务表。

#### 字段字典

下表按物理表列顺序列出全部字段。「可空」表示允许SQL NULL；12个数值字段使用TEXT保存规范十进制数值，保留来源数值精度，去掉无意义的末尾零，不经二进制浮点转换、不缩放。原始JSON字面格式以归档原文为准。

| 字段 | SQLite类型 | 可空 | 来源字段／写入依据 | 含义、单位及约束 |
|---|---|---|---|---|
| `id` | INTEGER | 主键 | SQLite分配 | 物理事实行ID；每次真实修订使用新ID |
| `observation_date` | TEXT | 否 | `SOLAR_DATE` | 来源观察日，规范为`YYYY-MM-DD`；不是取得时间、写入时间或GDP报告期 |
| `definition_version` | TEXT | 否 | 采集定义版本 | 当前为`v1`；与观察日共同区分逻辑事实身份，不同定义不混读 |
| `china_2y` | TEXT | 是 | `EMM00588704` | 中国2年期国债收益率，百分数（`1.65`表示1.65%） |
| `china_5y` | TEXT | 是 | `EMM00166462` | 中国5年期国债收益率，百分数 |
| `china_10y` | TEXT | 是 | `EMM00166466` | 中国10年期国债收益率，百分数 |
| `china_30y` | TEXT | 是 | `EMM00166469` | 中国30年期国债收益率，百分数 |
| `china_10y_2y_spread` | TEXT | 是 | `EMM01276014` | 中国10年期减2年期收益率利差，百分点；保存来源披露值，不另用两个期限重算 |
| `china_gdp_yoy` | TEXT | 是 | `EMM00000024` | 来源标注的中国GDP年增率，百分数；按返回观察日保存，不推定为每日发布或自行补出报告期 |
| `us_2y` | TEXT | 是 | `EMG00001306` | 美国2年期国债收益率，百分数 |
| `us_5y` | TEXT | 是 | `EMG00001308` | 美国5年期国债收益率，百分数 |
| `us_10y` | TEXT | 是 | `EMG00001310` | 美国10年期国债收益率，百分数 |
| `us_30y` | TEXT | 是 | `EMG00001312` | 美国30年期国债收益率，百分数 |
| `us_10y_2y_spread` | TEXT | 是 | `EMG01339436` | 美国10年期减2年期收益率利差，百分点；保存来源披露值，不另行重算 |
| `us_gdp_yoy` | TEXT | 是 | `EMG00159635` | 来源标注的美国GDP年增率，百分数；不另行推断同比／年化口径或报告期 |
| `content_hash` | TEXT | 否 | 规范观察日及12项数值 | 上述内容JSON的SHA256，64字符十六进制文本；不包含ID、修订号、来源引用和写入时间，用于识别数值变化，不是原始响应文件哈希 |
| `version` | INTEGER | 否 | 同日、同定义的前一修订号加1 | 首版为1，必须大于0；旧修订保留 |
| `is_latest` | INTEGER | 否 | 发布事务维护 | 只能为0或1；1为同日、同定义当前版本，0为历史版本 |
| `batch_id` | INTEGER | 否 | `collection_batches.id` | 形成此事实版本的发布批次；外键引用公共批次 |
| `source_id` | INTEGER | 否 | `collection_sources.id` | 形成此版本的原始分页响应；与`batch_id`组成复合外键，保证来源属于同一批次 |
| `updated_at` | TEXT | 否 | 发布事实时的实际UTC时间 | 带时区的ISO时间；只在新增事实／修订时写入，不因重复检查刷新旧行时间 |

#### 主外键、索引与读取

- 主键为`id`；唯一约束`(observation_date, definition_version, version)`防止修订号重复。
- 部分唯一索引`sovereign_yield_latest`约束`(observation_date, definition_version) WHERE is_latest=1`，保证每个日期、定义最多一个当前版本。
- 范围索引`sovereign_yield_dates`为`(observation_date DESC, is_latest)`。日常读取筛选`is_latest=1`及适用的`definition_version`，按观察日查询；历史审计保留全部修订。
- `batch_id → collection_batches.id`；`(source_id, batch_id) → collection_sources(id, batch_id)`。提供方`eastmoney`、dataset、原始文件路径、双哈希和真实取得时间通过来源／批次读取，不在事实行重复存储。

```sql
SELECT f.observation_date, f.china_10y, f.us_10y, f.version,
       f.updated_at, s.obtained_at, s.source_system, s.dataset
FROM sovereign_yield_observations AS f
JOIN collection_sources AS s ON s.id = f.source_id AND s.batch_id = f.batch_id
WHERE f.is_latest = 1 AND f.definition_version = 'v1'
ORDER BY f.observation_date DESC;
```

数值列读取结果仍为文本；需要数值计算时使用十进制解析，不按文本进行数值排序或直接用浮点转换替代无损读取。

#### 修订、缺值与时间口径

相同日期和定义的12项数值未变化时，不新增版本、不改原来源或`updated_at`；新的检查证据仍保留在公共来源／批次中。数值变化（包括NULL补为有效值）在同一事务将旧版`is_latest`改为0并插入新版。任一已有非空字段变空时，整条候选暂不替代旧事实，候选日期／字段／source登记在批次结果；之后优先复查并记录解决批次及来源，不把旧字段拼入新响应。

有效零和负值保留；来源null或空字符串记为NULL，不能当作零，也不使用前值、插值、另一国家或另一来源补齐。同一观察日的中美字段可以分别缺失。非数值、非有限数或必需来源字段缺失属于校验失败，不能作为有效空值发布。

来源取得时间为`collection_sources.obtained_at`，事实写入时间为`updated_at`；维护页取两者的真实最大时间。观察日不代替更新时间；空表显示暂无数据，缺少可用时间时显示未记录。重复检查时间及逐字段首末非空日见公共批次结果，不能通过事实行更新时间推断最近一次检查。

年度闭区间分页执行、初始化清单、近期检查新鲜度及逐字段覆盖均保存在公共采集批次。GDP字段仅记实际非空观察，不套日度收益率的发布频率；尚无可靠中美发布日历，因此`coverage_status=unknown`不代表逐日完整，日历间隔也不能直接认定漏采。采集和恢复入口及限制见[指数页国债收益率采集](index-data-collection.md#中美国债收益率采集)。表结构和数值规则分别以[v39迁移](../stock_analysis/infrastructure/migrations/039_sovereign_yields.sql)及[采集实现](../stock_analysis/domains/market/bond_yields.py)为准。

## 3. 更新、缓存与价格一致性

### 3.1 数据源与同步范围

| 数据 | 当前来源 | 普通更新 | 首次、手动全量或核查到期 |
| --- | --- | --- | --- |
| 财务三表及关键指标 | 新浪 `CompanyFinanceService.getFinanceReport2022` | 每类从最近 2 期开始，必要时继续分页覆盖已有尾部 | 分页获取完整可用历史 |
| 分红 | 东方财富 `RPT_SHAREBONUS_DET` | 公告日和除权日分别回看 30 天，复查未完成预案；必要时全量补偿 | 获取全部事件 |
| PE、PB、市值 | 百度 `opendata` | 来源最小取近一年，普通更新只合并最近三个月；缺口超过三个月时向前接回成功水位，必要时扩大来源窗口 | 取近十年、近五年、近三年三个窗口，保留实际取得的观察 |
| 行业估值 | 东方财富 `RPT_PCF10_INDUSTRY_CVALUE` | 获取当前行业快照，内容变化才新增行 | 仍为当前快照，不提供完整历史行业序列 |
| 融资 | 东方财富 `RPTA_WEB_RZRQ_GGMX` | 从最后成功水位回看 14 个自然日，一直到本次日期 | 获取全部可用历史 |
| 股东人数 | 东方财富 `RPT_F10_EH_HOLDERNUM`、`RPT_HOLDERNUM_DET` | 两个来源分别按公告日期增量，从上次成功检查往前回看 30 天，补到本次日期 | 首次或手动全量获取完整记录 |
| 不复权股价 | 腾讯日 K | 回看最多 30 个已有交易日并补充新日期 | 从 `1990-01-01` 请求全部可用历史 |
| 前复权股价 | 腾讯日 K | 回看最多 20 个已有交易日；历史超过 40 条时还检查默认两个旧锚点 | 请求完整历史并校验；历史变化也会触发重建 |

“完整历史”始终受数据源实际覆盖范围限制，不意味着可以取得来源未提供的年份。融资增量以成功水位为基准，而不是简单只取“今天前 14 天”，长期未访问的缺口会一并补抓。

增量范围与实际下载量需分别看待：

- 财报普通分页每页 2 期，直到重新覆盖数据库最近两期，长期离线期间的新报告会一起补齐；跨页后重查首页，避免来源更新造成漏页。TTM 仍使用完整已存历史。
- 估值来源只支持近一年、三年、五年、十年。普通更新的合并起点取“今天向前推三个日历月”与“最后保存的观察日”中更早的日期，确保不截掉长期缺口；大窗口下载后仍只合并需要的范围，较早记录保留。来源回看范围缺失已知观察日则拒绝提交。三个月裁剪减少写入，不减少当前百度接口下载量。
- 腾讯行情请求条数按起止日期之间的自然日跨度保守估算，每页上限 640 条。正常短区间不再固定下载 640 条；单日历史锚点请求 1 条。长历史仍向前分页，页间至少保留 20 个交易日重叠校验，尾页也缩小请求量。
- 股东人数的公告窗口以最后成功检查时间为起点回看 30 天，不是只查询“今天前 30 天”；长期未访问的新公告会一并查询。更正若仍保留窗口外的旧公告日期，日常增量无法发现，需要手动全量核查。

缓存检查和全量核查是两个不同规则：前者决定是否访问来源，后者决定访问时查询多大的历史范围。

| 数据 | 自动来源检查 | 周期性全量核查 | 其他扩大范围的条件 |
| --- | --- | --- | --- |
| 财务三表及关键指标 | 成功检查后 24 小时 | 成功全量后 180 天 | 首次、手动全量；普通查询从近期 2 期开始并按需分页 |
| 分红 | 成功检查后 24 小时 | 无 | 首次、手动全量，以及增量事件无法对应已有事件时补偿；持续复查未完成预案 |
| PE、PB、市值 | 成功检查后 24 小时 | 成功全量后 90 天 | 首次、手动全量；长期离线或窗口无交集时扩大来源窗口 |
| 行业估值 | 成功检查后 24 小时 | 无 | 只查询当前快照，内容变化才增加记录 |
| 融资 | 每个北京时间自然日一次 | 无 | 首次、手动全量；增量漏掉已知日期时尝试一次全量补偿 |
| 股东人数 | 每个北京时间自然日一次，两个来源独立记录成功时间 | 无独立的定期全量任务 | 首次或手动全量取完整记录；日常按公告日期发现新公告和近期更正，增量无新记录仍保留完整历史 |
| 不复权股价 | 每个北京时间自然日一次 | 无 | 首次、手动全量；增量漏掉已知日期时尝试一次全量补偿 |
| 前复权股价 | 每个北京时间自然日一次 | 成功全量后 30 天，作为变化检测的兜底 | 首次、手动全量；近期重叠段或旧锚点变化、响应漏掉已知日期时重建 |

按天检查在北京时间 00:00 切换，不是滚动等待 24 小时；当天成功检查过后再打开股票不会自动重抓，下一天首次访问时才检查。市场数据在当天检查后仍可能继续变化，若需要盘中或收盘后的最新数据，可手动刷新。失败不会把成功检查时间推进到今天，后续访问可重试。

显式“刷新数据”绕过普通缓存判断；股价保留 5 秒短期复用及并发复用保护。“全量刷新”仍对当前股票请求各模块的全量范围。全量核查到期是下一次访问/更新时触发，不是定时任务。财报和估值普通增量更新不延后全量核查期限；既有库已保存的旧核查日期继续有效，下一次成功全量后才按新的 180/90 天周期设置。

股东人数每天检查公告日期增量，并非订阅或实时监测公告。F10 来源按 `NOTICE_DATE`，明细来源按 `HOLD_NOTICE_DATE` 筛选；不以统计截止日限制增量，所以新公告可以更新很久以前的统计记录。首次、手动全量仍核验完整历史；增量响应不要求包含全部旧统计日，也不删除未返回的旧记录。无新公告时推进成功检查时间，保留既有数据状态。财报历史更正目前也依靠近期重抓、低频全量及手动核查发现，不声称已自动识别更正公告。

正常打开页面先读取本地事实，不在 HTML 生成过程中请求外部数据。前端随后只更新需要检查的数据；所有缓存有效时不会显示“更新中”或发起多余价格同步。

顶部“全量刷新”在用户确认后，为当前股票的财务、分红、估值、筹码和股价请求传递全量参数，不会批量刷新其他股票。不同数据集独立提交；一次全量刷新不是覆盖所有数据集的单个事务，可能部分成功、部分失败。

### 3.2 同步状态与失败处理

单个数据集的处理顺序为：登记任务和当前修订号 → 事务外获取与验证来源数据 → 短事务内同时写事实、完成任务并推进状态。

`sync_state` 的几个时间和范围字段含义不同：

| 字段 | 含义 |
| --- | --- |
| `checked_at` | 用于缓存判断的成功来源检查时间；旧导入可保留原文件的历史检查时间 |
| `succeeded_at` | 当前成功同步的提交时间；财务缓存更新时间根据相关成功事实计算 |
| `data_watermark` | 已成功接受的最新数据日期，用于增量起点 |
| `coverage_start/end` | 提交后整个数据集的成功覆盖边界 |
| `next_full_audit_at` | 按数据集策略设置的下次全量核查时间；不参与周期全量的数据集提交后留空，旧期限在这些数据集上不再触发全量 |
| `revision` | 乐观并发控制修订号；旧任务不能覆盖更新任务的成功提交 |

`sync_runs.record_count` 是本次接受的记录数，不是数据库总行数。来源失败、候选校验失败或提交失败时，不推进成功水位；过期任务记为 `superseded`。明确“无数据”可以用于初始化，但不能覆盖一个已有有效数据的数据集。

不同服务各自保留已保存事实并返回警告。启动时的中断恢复只处理遗留任务与候选版本，不回滚已经成功提交的事实。

### 3.3 共享价格版本

所有图表使用同一套共享日价。前复权完整版本通过候选阶段校验，发布后不可逐日改写；活动指针必须指向同股票、来源和复权口径的已完成版本，并与覆盖范围一致。

价格未变化时复用版本；新增日期或历史复权基准变化时发布新版本。新版本发布后尝试清理：保留最近两个完成版本，并保留仍是活动版本或仍有有效租约的更旧版本。不是无条件“最多两份”，也没有定时清理线程。

固定版本读取会续租 5 分钟；读取已经被清理的版本时 API 返回 HTTP 409，前端会重新获取价格上下文。价格同步与财务、估值、筹码事实更新独立，价格失败不会撤销它们已提交的事实。

## 4. 数据库结构与事务保障

迁移文件位于 `stock_analysis/infrastructure/migrations/`：

| 版本 | 文件 | 主要变化 |
| --- | --- | --- |
| 1 | `001_initial.sql` | 股票、同步状态、筹码、日价和前复权版本基础结构及来源约束 |
| 2 | `002_shared_data.sql` | 旧 JSON 导入记录、价格版本租约 |
| 3 | `003_p4_facts.sql` | 财报、分红、估值、行业、覆盖项和旧月度快照；导入身份改为 `(path, dataset)` |
| 4 | `004_financing_compact.sql` | 配合 `financing_storage.migrate_financing()` 精简融资 JSON |

既有迁移内容不可修改，升级应新增编号迁移。应用校验数据库标识、迁移文件名和内容哈希、迁移记录及 `user_version`；未知或被改动的结构会拒绝访问。校验和按统一换行后的文本计算，兼容 Git 的 LF/CRLF 转换。

已有结构需要升级时先创建迁移前备份；待执行迁移在事务内逐条运行，失败回滚。版本 4 会重建融资表，验证原值和沿用来源，恢复原表索引与来源触发器；若发现当前原始字段的旧来源标记冲突，会生成 `financing-migration-audit-*.json`。审计标记的是提交前验证阶段，是否升级成功仍以数据库迁移记录为准。

日常连接按线程创建，退出时关闭：默认只读连接开启一个读取事务，写连接使用 `BEGIN IMMEDIATE`；启用外键、默认 5 秒忙等待及 `synchronous=FULL`。事实表、同步日志和活动版本之间的来源关系由约束、触发器及 `Database.check()` 共同验证。

journal 模式由所加载的 SQLite 运行库决定：`auto` 只有在代码认可的 WAL-reset 修复版本上选用 WAL，否则使用 DELETE；显式要求不受支持的 WAL 会报错。代码认可 `>=3.51.3`、`3.50.7 <= 版本 < 3.51.0`、`3.44.6 <= 版本 < 3.45.0`。本机核对时 SQLite 为 **3.45.1**、模式为 **DELETE**，不能据此推断另一台电脑的运行模式。

## 5. 备份机制

### 5.1 备份类型、触发与保留

专项备份默认放在数据库同目录下的 `backups/`，日常库即 `data/backups/`；默认日备份单独放在其 `stock_cloud_bkp/` 子目录。

| 类型 | 触发时机 | 文件名与保留规则 |
| --- | --- | --- |
| 每日更新前快照 | 后台启动时调用 `daily_backup()`，或后台跨天后第一次调用业务更新服务时 | `backups/stock_cloud_bkp/stock_analysis-daily-YYYY-MM-DD.sqlite3`；当天没有则创建，已经存在则保留当天首次快照，不因重启或业务提交覆盖 |
| 结构升级前备份 | 非空旧库需要执行新迁移时 | `pre-migration-<UTC时间戳>.sqlite3`；不自动轮转 |
| 旧筹码导入前备份 | 旧融资或股东 JSON 首次实际导入前 | `pre-import-<数据集>-<股票>-<哈希前缀>.sqlite3`；不自动轮转，并另存旧 JSON 的 `.migration-*.bak` |
| 手动或专项备份 | 手动备份命令、维护工具主动调用备份 API | 由调用者命名，例如 `manual-*.sqlite3`、`pre-refresh-all-*.sqlite3`；不自动轮转 |

财报、分红、估值/行业、筹码和价格更新服务在写入前调用 `backup_before_update()`。同库并发调用通过备份锁串行检查：当天快照已经存在就立即返回，不复制整库、不重复完整校验；尚不存在时先创建并校验，再允许服务更新。后台跨天运行也会检查新的 UTC 日期，不必重启。普通刷新、全量刷新以及切换股票后的自动更新都不会覆盖当天已有快照。直接调用数据库写接口不会自动备份。

默认日备份目录 `backups/stock_cloud_bkp` 只保留最新一个有效日期文件，供云同步使用。新日期备份成功创建后，目录内其他有效日备份移动到 `backups` 根目录作为历史归档；根目录已有同名文件时，新增归档使用带 `cloud` 与 UTC 时间的名称，不覆盖原文件。默认日备份与根目录中所有匹配当前数据库及有效日期的日备份合并计算，按日期最多保留最近 `keep=7` 份，较旧记录自动删除；专项迁移、导入及恢复前备份不参与日备份数量限制。显式传入自定义目录的 `daily_backup(directory=...)` 仍按 `keep` 参数清理该目录中属于当前数据库的旧日备份。

备份日期使用 **UTC**。北京时间每天 **08:00** 才进入新的备份日期，例如北京时间 2026-10-01 07:30 的日备份仍命名为 `2026-09-30`。

### 5.2 如何取得一致快照

`Database.backup()` 使用 SQLite Online Backup API，从只读源连接复制到目标目录的临时文件，可以在后台运行时备份，不依赖直接复制正在写入的数据库文件。

生成完成后，备份统一使用自包含的 DELETE journal 模式，执行结构、完整性、外键及相关业务关系检查，再发布到目标路径。常规备份目标必须是一个新文件，不能是原数据库或已存在的文件；发布使用同目录临时文件的硬链接，文件系统不支持时会报错。

日备份保留创建时的已提交状态，当天不替换。通常代表当天第一次业务更新前的状态；文件名日期是快照创建日，不是其中每条数据的日期。首次启动快照在中断恢复和旧缓存导入之前生成，结构升级仍由独立迁移前备份保护。现有同日旧机制生成的备份也会保留，不尝试重建已经丢失的当天早期状态。

### 5.3 失败与边界

- 每日首份备份创建失败时，本次更新报错且不开始业务写入；之后调用可以重试。启动阶段的备份异常会使本次后台启动失败。已有快照在更新路径不反复核验，启动时仍会核验；发现损坏后应人工处理，不能自动覆盖历史恢复点。
- 恢复日快照会回退快照生成后的当天变更，包括新增来源数据及人工覆盖项。重要操作前仍可创建独立命名的手动备份；“全量刷新”按钮不会另建 `pre-refresh-all-*` 备份。
- 没有固定时刻的定时备份线程。后台持续运行、没有相关更新服务调用时，不会每天自动新建备份；没有运行或更新的日期不补齐。
- 当前默认备份与主库位于同一块本地磁盘，没有内置云备份或异地同步。日备份用于数据恢复，不能覆盖整块磁盘损坏的场景。
- 长期保留的手动、升级前和导入前备份需要另行管理空间；项目测试清理不能删除这些正式备份。

## 6. 检查、手动备份与恢复

以下命令在项目根目录执行。维护入口 `stock_analysis.infrastructure.storage` 使用 Python 标准库；检查和在线备份可在后台运行时执行，初始化/迁移与正式库替换需要先停止同库后台。

```powershell
# 核验结构、SQLite完整性、外键及业务来源关系，不更新数据源。
python -m stock_analysis.infrastructure.storage check

# 初始化或升级；同库后台运行时会被实例锁拒绝。
python -m stock_analysis.infrastructure.storage init
```

### 6.1 创建独立手动备份

```powershell
$backupStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$manualBackup = "data/backups/manual-$backupStamp.sqlite3"
python -m stock_analysis.infrastructure.storage backup --output $manualBackup
```

输出路径必须尚不存在，重复使用同名文件会失败。备份包含全部应用表及状态，不是单只股票的导出。

### 6.2 恢复到新文件并核验

以下示例沿用上一步的 `$manualBackup` 和 `$backupStamp`，恢复目标是本地验证目录中的新文件：

```powershell
$restorePath = "data/verification/reports/storage/restored-$backupStamp.sqlite3"
python -m stock_analysis.infrastructure.storage restore --database $manualBackup --output $restorePath

# 与当前结构版本匹配时，执行完整检查。
python -m stock_analysis.infrastructure.storage check --database $restorePath
```

恢复命令会锁住目标路径，并拒绝已有目标及其 SQLite 辅助文件，**不会覆盖或自动切换当前正式数据库**。备份本身使用与其历史结构匹配的检查；如果恢复的是旧版本，可在没有后台使用恢复库时运行：

```powershell
python -m stock_analysis.infrastructure.storage init --database $restorePath
python -m stock_analysis.infrastructure.storage check --database $restorePath
```

正式切换需要另行执行：停止 8765 后台并确认退出 → 给当前正式库创建独立备份 → 核验恢复库 → 在没有活动连接的情况下替换正式文件 → 重启后台并确认页面、结构和数据读取正常。不要直接覆盖活动 `.sqlite3`，也不要遗漏 WAL 模式下尚未处理的辅助文件。

### 6.3 空间管理

旧 JSON、正式备份与当前数据库分别占空间。融资结构精简或删除旧价格版本后，数据库文件未必立即缩小；空闲页通常可被后续写入复用。

日常启动不自动执行 `VACUUM`，维护 CLI 也没有单独的 `vacuum` 子命令。若需实际收缩文件，应先备份，停止同库后台，并在持有实例锁的维护流程中执行，再核验和恢复服务。空间整理不能删除业务历史来替代。

2026-10-12在点位索引重建后，已正式备份、停止8765并持有实例锁执行VACUUM。日常库文件由1314324480字节降至1304301568字节，实际释放10022912字节（约9.56 MiB），空闲页由2413降至0。67张表的行数和逐行内容指纹、全部表／索引／触发器定义前后一致，完整性与外键检查通过；8765已恢复，页面、目录和维护接口均返回200。验收及维护统计分别见 `data/verification/reports/2026-10-12-sqlite-vacuum.json` 和 `2026-10-12-sqlite-vacuum-maintenance-api.json`，后台日志位于 `data/verification/logs/daily-8765-vacuum-20261012.*.log`。

2026-10-14按用户要求再次整理日常库。先创建并检查正式备份 `data/backups/pre-vacuum-20261014T1430.sqlite3`（1,589,465,088 字节），核对并停止原 8765 进程后持数据库实例锁执行 VACUUM。数据库从 1,589,465,088 字节降至 1,440,706,560 字节，释放 148,758,528 字节（约 141.9 MiB），page_count 从 388,053 降至 351,735，freelist 从 34,501 降为 0。68 张表共 3,198,020 行的逐表指纹与备份一致，schema 定义一致，完整性检查为 `ok`、外键错误 0；`Database.check()` 前后均通过。8765 已恢复为 PID 12704，首页、维护统计、行业状态/数据、财报 API 均 HTTP 200。详见 `data/verification/reports/20261014_sqlite_vacuum.json` 和 `data/verification/logs/daily-8765-vacuum-20261014.*.log`。

## 7. 代码依据与历史记录

| 内容 | 代码依据 |
| --- | --- |
| 表结构、索引与触发器 | [迁移目录](../stock_analysis/infrastructure/migrations/) |
| 迁移、连接、同步提交、检查、备份及恢复 | [storage.py](../stock_analysis/infrastructure/storage.py) |
| 启动顺序、页面缓存、各数据 API | [server.py](../stock_analysis/http/server.py) |
| 筹码事实、增量范围与备份触发 | [update_service.py](../stock_analysis/domains/stocks/update_service.py) |
| 融资精简与沿用来源检查 | [financing_storage.py](../stock_analysis/domains/stocks/financing_storage.py) |
| 共享价格、版本发布及租约清理 | [price_service.py](../stock_analysis/domains/stocks/price_service.py) |
| 财报、分红与估值更新 | [fundamental_service.py](../stock_analysis/domains/financials/fundamental_service.py)、[dividend_service.py](../stock_analysis/domains/stocks/dividend_service.py)、[valuation_service.py](../stock_analysis/domains/valuation/valuation_service.py) |
| 图表范围、频率及刷新操作 | [web/pages/index.html](../web/pages/index.html) |
| 全字段交易日快照、季度恢复与来源回放 | [market_trade_snapshot.py](../stock_analysis/domains/market/market_trade_snapshot.py)、[字段字典](../stock_analysis/domains/market/resources/market_trade_snapshot_fields.json) |
| 公共来源归档、同源预算与冷却 | [collection.py](../stock_analysis/collection/collection.py) |

历史实测参见 [P4/P5 验收](data-sources/p4-acceptance.md)、[融资存储精简验收](data-sources/financing-compaction-acceptance.md)。这些记录用于说明特定日期的测试结果，不应代替当前代码规则。

### 新浪关键指标原始数据

财务抓取同时独立同步 `gjzb`，沿用 `financial_reports` 表和 `financial:gjzb` 同步水位，无需结构迁移。旧库首次运行时补齐关键指标完整历史，后续与三表采用相同的 24 小时检查、近期 2 期增量及定期全量机制；手动刷新会检查四类来源。抓取失败或丢失已有有效科目时保留已存记录并返回警告，不阻断其他类型成功提交。原始 JSON 保留所有来源科目、分组、重复项、单位/显示格式及来源元信息，暂不映射至图表和 AI 指标；加权 ROE 和来源 ROIC 不替换既有估算口径。

版本 33 新增 `stock_announcement_identities`（巨潮股票组织身份）、`stock_announcement_metadata`（公告筛选元数据版本）和 `report_disclosure_dates`（报告期日期判断版本）。三表保存身份/公告/判断关联及真实取得、验证时间；公告来源通过公共 `collection_sources`/`collection_batches` 追溯，sync run 记录成功检查范围。个股策略为 `metadata_only`：不保留响应正文、响应哈希、PDF 或正文片段。日期判断按规则版本和当前标记保留历史修订，人工 `report_overrides` 仍在读取末尾优先。时间展示采用各表 `obtained_at`、`updated_at`、`created_at`、`validated_at`，结果表通过 `run_id` 关联任务完成时间；没有成功检查时显示未记录。默认类别覆盖年报、半年报、一季报、三季报、业绩预告及权益分派；类别 profile 写入 sync run/batch 范围，扩展类别会重新覆盖目标范围。请求范围下限不早于上海当前日期前十个日历年，已验证覆盖水位不代表公告网站全部历史。

## v40 统一采集管理

`collection_schedules`保留原五列，不重写拥挤度配置。新增两张运行与配置表：

|表|列／用途|时间依据|
|---|---|---|
|collection_schedule_rules|task_key主键且外键引用collection_schedules；rule_json日周月规则、params_json白名单参数；无原始响应|updated_at真实保存时间|
|collection_schedule_slots|task_key+slot_key主键；status running/complete/failed/pending、operation_ref引用既有领域运行、attempts有限恢复次数、error；不复制进度或事实|started_at、finished_at、updated_at真实领取／结束／状态更新时间|

运行引用可以是index operation ID或sw_update_runs:id，跨类型引用不建立伪外键；原领域历史不重写。槽位状态及operation_ref各有索引，行业action/id和指数dataset/started_at增加轻量历史查询索引。sw_update_runs新增cancel_requested（默认0）仅用于定时安全停机，未开放行业用户取消；原任务时间仍使用started_at/finished_at。

两张新表在maintenance.DESCRIPTIONS登记「运行与配置」。TIME_COLUMNS自动识别上述真实字段，空表／缺时间显示暂无数据／未记录，slot_key和目标报告期不作为更新时间。公共batch通过槽位operation_ref识别真实scheduled触发；来源文件策略及数值精度不变。

隔离库1000条代表性槽位实测含索引258048字节（258.048字节/行），规则仅1行分配8192字节（含主键索引，页分配下限，不作稳态行大小估计）；1000条领域记录+槽位写入约17ms，任务目录常用读取约5—7ms。机器与规模相关，实测及查询计划见[data/verification报告](../data/verification/reports/collection_management_20261025.json)。维护统计已验证新表发现、分类、用途、数量、大小、真实时间及外键。设计与限制见[统一采集管理](collection-management.md)。
## 航运行业研究（v41）

`research_shipping_rate_points`：指数/频率/原航线名/统计类型/起止区间/来源通道定义事实身份，保存数值、单位、公布变化、披露日、质量及修订链；source_id/batch_id组合外键关联公共证据，不逐行复制URL、正文或哈希。全年与YTD均值独立保存，Excel与官方通道不覆盖彼此。

`research_shipping_report_metrics`：正式document/parse、指标、集团范围、累计口径、单位/币种、证据页及提取版本；单季差分和单箱收入读取时计算。`research_shipping_industry_metrics`：港口吞吐量、指标期、报告月、披露日、单位、同比与来源修订；scope分别保留全国/沿海/内河及原文规模以上/主要限定，basis区分monthly当月、ytd年初累计、annual全年，period为统计区间终月，不将累计量冒充当月值；现有行业表无适用港口统计口径，单设此表，不混入运价或公司财务。

`research_shipping_forecast_versions`：601919专用目标季度、名称、模型和输入hash、紧凑输入引用/参数/结果快照；禁止UPDATE，保存新版本而非覆盖历史。四表均为「行业研究」，维护时间来自真实created_at；空表/空时间不以报告期或观察日补齐，显示未记录。业务表无完整源库副本，PDF/解析复用正式存储，原文归档与来源预算复用公共表。细节见[航运预测](shipping-forecast.md)。


## 可调采集限速（v42）

collection_rate_limits以(task_key,provider)复合主键保存settings_json和真实updated_at，一项配置一行，不重复来源或事实。仅保存七个可调数值，不改变数值精度；60秒窗口、冷却及重试规则不作为自由配置。当前九个网络任务／来源组合含索引占8192字节（表4096、索引4096；页开销下约910字节/行），参数JSON平均110字节；1000次逐笔保存约8760ms，九任务列表读取约3.5—4.1ms，本机单次测量。主键命中点查；真实应用策略只在公共批次scope_json中保存一次紧凑快照，不写到每条事实。隔离维护统计已核对用途、分类、数量、大小和真实更新时间；空表时间为空。[复测报告](../data/verification/reports/collection_rate_limits_20261025.json)。

目录整理后，v32 财务来源升级和 v37 指数价格升级分别由 `stock_analysis/domains/financials/schema_upgrade.py` 与 `stock_analysis/domains/indices/schema_upgrade.py` 执行；storage 在原版本分支中延迟调用。SQL 历史文件、顺序、校验和及 schema 版本保持。服务运行/恢复重载循环位于 `stock_analysis/runtime.py`，业务初始化入口仍在 HTTP server。
