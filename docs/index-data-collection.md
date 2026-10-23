# 指数行情、估值与拥挤度数据说明

本文是指数点位字段、估值采集、市场拥挤度及其共享采集基础设施的统一说明。点位与估值分表；拥挤度保留独立指标身份。它们复用指数目录、公共来源归档和采集operation，但不跨来源、定义版本或指标身份拼接。页面读取只访问本地数据；采集只由明确操作触发，已授权的拥挤度日程除外。

当前SQLite结构版本为v39。点位v2只适用于中证历史与申万分析，新增字段以明确映射和单位证据写入；乐咕/Sina继续使用原v1定义和hash。以下早期部署记录保留当时状态，后续版本差异见字段表和当前实现。

### v38紧凑存储与读取

行情物理表仍一序列/日期一行，保留全部行情字段与溯源；观察日改整数日、SHA256改完整32字节、updated_at改可逆整数微秒（历史非规范ISO原串保留），source_id关联恢复batch_id/obtained_at，is_latest逻辑恒为1。表保留2个必要索引，日期查询转换参数，不在索引列上套转换函数。说明中的日期、来源/批次和时间均为逻辑含义；物理列及兼容读取规则见[SQLite v38说明](sqlite-storage.md#v38-指数行情紧凑存储)。

`field_lineage_json`物理为固定格式2数组；公式/口径/自身身份/来源真实时间通过固定规则和现有表恢复，计算时来源/输入值/原始修订及分母事实ID/version独立保存，不绑定未来修改后的当前输入。未知或撤回证据用显式fallback完整保存。批量展开父引用，正常图表/数值业务不展开溯源。`read_prices(fields=('close',))`仅取所需数值和ISO日期（Beta使用此路径）；默认完整读取仍恢复原逻辑列和完整溯源说明，有额外还原成本。市场统计/图表、模型公式及网络预算未改变。

### 阅读顺序

1. [总览与表身份](#1-总览与表身份)
2. [来源字段与行情身份](#2-来源字段行情身份与归档复用)，及估值字段表、拥挤度字段表。
3. [采集与历史回放](#history-replay)；[依赖检查和派生流程](#4-行情依赖与派生调度)。
4. [拥挤度日程与归档策略](#5-拥挤度来源日程与图表)。
5. [读取及图表行为](#reading-and-charts)。
6. [维护与验收](#maintenance-and-acceptance)。

各域仅保留一份当前规则；带日期的旧部署记录用于追溯当时验收，不作为现行行为说明。

## 1. 总览与表身份

| 数据域 | 主要事实表 | 业务身份 | 定义／收益口径 | 来源与时间字段 |
|---|---|---|---|---|
| 行情点位 | `index_price_observations` | `series_id + observation_date`物理唯一，version仅为同行修订计数；series由指数、provider、series_key、frequency、return_type、definition_version确定 | 中证/申万扩展为v2；Legu/Sina保持v1；收益口径不合并 | v38以`source_id`外键关联来源，恢复批次及真实`obtained_at`；`updated_at`解码为事实发布/修订时间 |
| 指数估值 | `index_valuation_observations` | `series_id + observation_date + version`；series由指数、provider、series_key、frequency、definition_version确定 | `index_metric_definitions`保留指标映射、利润期间、股本口径、统计方法和缺值规则 | 同样引用公共source/batch；事实`obtained_at / updated_at`为实际来源/写入时间 |
| 大盘拥挤度 | `market_indicator_observations` | indicator、scope、provider、channel、frequency、definition_version、日期、version | Legu API、HTML与BigQuant本地估算分开；BigQuant使用`amount_rank_top5_v1` | source/batch外键；`obtained_at / updated_at`为采集或本地计算/发布时点 |
| 公共请求 | `collection_batches / collection_sources / collection_provider_state` | batch按任务、来源、范围、触发与定义版本；source按实际请求/尝试 | 证据先归档、校验后按短事务发布；来源预算和冷却按真实provider共享 | batch `started_at / finished_at`，source `obtained_at`；SQLite备份不含原文文件 |

`index_price_definitions.updated_at`是定义登记时间，`index_price_series.created_at`是序列建档时间。指数行情v37只扩展现有事实表，不增加新表；拥挤度历史迁移v36创建自己的事实及日程表。所有表的维护用途和更新时间依据见[SQLite存储说明](sqlite-storage.md)及[后台维护页](database-maintenance.md)。

## 2. 来源字段、行情身份与归档复用

<a id="指数点位存储与归档复用"></a>

实现为 `quarterly_dashboard/index_prices.py`，历史导入为 `quarterly_dashboard/index_price_replay.py`。点位与估值分表，复用 `index_identities / index_catalog / index_provider_codes`，不进入个股价格表。宽基、申万行业、策略和后续主题指数共享结构；分类及层级继续由目录管理。

### 表与口径

| 表 | 内容 | 身份／时间依据 |
|---|---|---|
| `index_price_definitions` | 来源字段、点数单位、频率与缺值规则 | 提供方×接口序列×定义版本；`updated_at`为实际登记时间 |
| `index_price_series` | 指数身份、提供方、接口序列、频率、收益口径和定义版本 | 六字段联合唯一，事实引用整数ID；`created_at`为实际建档时间 |
| `index_price_observations` | 日期、OHLC及10个可空交易字段、修订、批次和来源 | 序列ID×日期物理唯一，就地更新当前值，不追加行情历史行；`obtained_at`为来源真实取得时间，`updated_at`为事实写入时间 |

日频／月频、不同提供方、接口和定义版本不拼接或覆盖。收益口径支持 `price / total_return / net_return / unspecified`；目前 `cn:000680` 科创综指明确为 `total_return`，其余序列保留 `unspecified`，不由类别猜测。以后确认收益口径时建立独立身份，不修改旧序列。[科创综指官方事实表](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000680factsheet.pdf)。

点数只接受有限正数；缺值为NULL，不由收盘点位生成开高低点。校验实际已有的OHLC关系。v2响应缺少字段键时，仅保留同一序列、同一观察日已发布修订中的该字段；不从前日或旧期填充。来源明确返回NULL不自动覆盖已有值，必须有核实过的撤回说明。字段存在但格式／数值异常时分段失败。数值或撤回原因变化才产生修订，重复响应不改原事实出处。估算合入同一行情行，以`field_lineage_json.estimates`明确标识；官方原值优先，不另外保存派生行情或v1业务副本。

<a id="field-mapping"></a>
### 行情字段来源映射与单位

| 中文含义／字段 | 目标表列 | 提供方、dataset与原字段 | 原单位→标准单位／换算 | 频率／口径 | 缺值及当前入库 |
|---|---|---|---|---|---|
| 开盘点位 `open` | `index_price_observations.open` | csindex `index.cs.history.open` | 指数点→指数点，原值 | 日频，历史交易日 | 中证v2；来源无值留NULL，不由OHLC生成 |
| 最高点位 `high` | `index_price_observations.high` | csindex `index.cs.history.high` | 指数点→指数点，原值 | 日频，历史交易日 | 中证v2；缺值留NULL |
| 最低点位 `low` | `index_price_observations.low` | csindex `index.cs.history.low` | 指数点→指数点，原值 | 日频，历史交易日 | 中证v2；缺值留NULL |
| 收盘点位 `close` | `index_price_observations.close` | csindex `index.cs.history.close`; sws `index.sw.analysis.closeindex`; legu PE/PB `index.legu.pe/pb.close` | 指数点→指数点，原值 | 中证/申万日频；乐咕月度移动观察 | 中证/申万v2，Legu v1；来源缺值留NULL |
| 成交量 `volume` | `index_price_observations.volume` | csindex `index.cs.history.tradingVol`; sws `index.sw.analysis.bargainamount` | 股→股×1；亿股→股×1e8 | 中证/申万日频 | 中证单位由官方普通IndexDetail确认；申万按文档单位；合法零保留 |
| 成交额 `amount` | `index_price_observations.amount` | csindex `index.cs.history.tradingValue`; 同行本地估算（申万） | 亿元→元×1e8；派生公式输出元 | 中证原始日频；申万派生日频 | 申万无直接来源字段；同日`bargainsumrate/100 × Legu total_amount_yi × 1e8`同行估算 |
| 涨跌点数 `change_points` | `index_price_observations.change_points` | csindex `index.cs.history.change` | 指数点→指数点，原值 | 日频 | 中证v2；替代来源无此字段则留NULL，不由收盘价计算 |
| 涨跌幅 `change_pct` | `index_price_observations.change_pct` | csindex `index.cs.history.changePct`; sws `index.sw.analysis.markup` | 百分数→百分数，原值（2.5即2.5%） | 日频，来源统计口径 | 中证/申万v2；负值合法 |
| 换手率 `turnover_rate` | `index_price_observations.turnover_rate` | sws `index.sw.analysis.turnoverrate` | 百分数→百分数，原值 | 日频；更细统计口径未披露 | 申万v2；不设100%上限，缺值留NULL |
| 成交额占比 `amount_ratio` | `index_price_observations.amount_ratio` | sws `index.sw.analysis.bargainsumrate`; 同行本地估算（中证） | 百分数→百分数；派生公式×100 | 申万/中证派生日频 | 中证按`amount/同日Legu total_amount×100`估算；>100拒绝不裁剪 |
| 成分数量 `constituent_count` | `index_price_observations.constituent_count` | csindex `index.cs.history.consNumber` | 个→非负整数个 | 日频 | 中证v2；仅整数，不从目录名单推算 |
| 均价 `mean_price` | `index_price_observations.mean_price` | sws `index.sw.analysis.meanprice` | 元→元/股，原值 | 日频；来源加权口径未进一步披露 | 申万v2；不是指数点位，缺值留NULL |
| 流通市值 `float_market_cap` | `index_price_observations.float_market_cap` | sws `index.sw.analysis.negotiablessharesum1` | 亿元→元×1e8 | 日频 | 申万v2；保留来源流通市值名称，不改称自由流通市值 |
| 平均流通市值 `mean_float_market_cap` | `index_price_observations.mean_float_market_cap` | sws `index.sw.analysis.negotiablessharesum2` | 亿元→元×1e8 | 日频 | 申万v2；定义细节未披露，保留来源名称和空值 |

| 字段来源及估算 `field_lineage_json` | `index_price_observations.field_lineage_json` | 本地发布/补算元数据 | JSON；字段单位沿用上表，分母denominator_cny为元 | 同一业务行 | _raw记录字段出处/稳定修订；estimates记录公式/父依赖/计算时间和估算标记，无估算时无对应标记 |

中证普通`IndexDetail`页面在官方脚本中用`tradingVol`原值并引用成交量单位“股”提示，`tradingValue`标注“亿元”；按普通股票指数详情组件确认，不把同bundle中特殊TaiBao页面的“万手”推广到该接口。可复核材料为[中证单位证据报告](../data/verification/reports/20261018_csindex_units_evidence.json)，其中保存官方脚本来源、组件选择、字段格式及双哈希校验信息。申万字段名及原始单位按已归档接口响应和AKShare文档字段表确认；细分换手率统计口径、流通市值口径未获进一步披露，因此不作推断。

中证/申万原series和事实行就地升级v2，保留原ID及OHLC；不保留v1行情业务副本。旧v1定义JSON作为小型历史口径记录保留，不是一份行情数据。Legu/Sina各自保持v1序列和OHLC hash，不合并不同来源、频率或收益口径。每个序列/实际观察日通过唯一索引只保留一行当前事实，version仅为修订计数。

估算中证成交额占比、申万成交额写入同一行的amount_ratio/amount，不建立provider=derived序列、独立batch/source或估算响应文件。`field_lineage_json._raw`记录稳定原始修订号及各有效字段source/batch/真实取得时间；`estimates`记录估算标记、原始目标NULL、公式、输入值/单位、父行情身份及原始修订、同日分母事实ID/version/provider/channel/source/batch/取得时间（Legu优先，授权20日可回退BigQuant）、分母元值、计算时间和口径差异。仅估算更新保留原行source_id/batch_id/obtained_at，updated_at为本次写入时间，computed_at为估算计算时间。重复来源值不变时不改原出处或增加修订；来源缺键继承仅限同日既有字段，官方有效值优先。输入修订后就地重算，依赖失效就地置NULL并保留撤回原因；不会保留陈旧估算或丢掉估算标记。

计算优先使用精确同日Legu总额；按用户2026-10-18确认，2026-07-31至08-27这20日缺值可回退已归档的BigQuant全A成交额汇总，同时补算中证amount_ratio和申万amount。仅更新原行情行，不新增行情行或来源；保留原字段及实际父事实ID/version/provider/channel、公式、分母与计算时间。其他日期不自动扩展回退。BigQuant不是Legu，估算溯源和页面说明明确区分，后续Legu有效值优先并触发重算。52个重叠交易日的既有Legu/BigQuant金额比较见[分母数值对照报告](../data/verification/reports/20261018_index_denominator_overlap.json)；该报告不能证明申万占比分母与Legu完全同口径。申万来源占比只到0.01个百分点，若T为同日Legu总额，比例舍入对反推金额的误差上限约为`±T×0.00005`，未计来源范围差异；例如T=15,690.66亿元、占比4.82%时估算756.289812亿元，舍入误差约±0.784533亿元。估算用于定性分析，不冒充官方原值或严格可比指标。

### 已有来源能力

| 数据集／provider | 原始点位字段 | 已映射字段 | 频率 | 序列 |
|---|---|---|---|
| `index.cs.history` / csindex | `open/high/low/close/tradingVol/tradingValue/change/changePct/consNumber`（字段可缺） | OHLC、volume、amount、change_points、change_pct、constituent_count | 日交易日；原单位/转换见字段映射表 | `history.points` |
| `index.sw.analysis` / sws | `closeindex/bargainamount/markup/turnoverrate/bargainsumrate/meanprice/negotiablessharesum1/negotiablessharesum2` | close、volume、change_pct、turnover_rate、amount_ratio、mean_price、float_market_cap、mean_float_market_cap | 日交易日；保留申万原口径 | `analysis.points` |
| `index.legu.pe` / legu | `close` | close | 月度移动观察 | `pe.points` |
| `index.legu.pb` / legu | `close` | close | 月度移动观察 | `pb.points` |
| `index.price.range_probe` / sina | 固定KLC解码得到OHLC | OHLC | 日交易日；仅已有归档 | `history.points` |

申万一、二级都有收盘点位及扩展交易字段；历史缺日、已停止指数及目录历史有效期／父级待核事项仍存在，不能用任务完成标记全窗口覆盖。策略共用同一结构：上证红利有中证日线，深证红利有乐咕月频，没有证据的策略日线保持缺口。中证看板／近期估值文件只有映射明确且响应包含可用点位的序列才发布；不能由它们补造历史行情。

乐咕保留实际观察日及同月移动快照，读取时按月返回查询范围内最后实际观察点，不转成日频。点位与估值首日可以不同；发布前回溯点位与发布后行情需要区分，第一条观测不作为成立日。

<a id="history-replay"></a>
### 历史回放

```powershell
python -m quarterly_dashboard.index_price_replay --start 2016-10-11 --end 2026-10-09
```

只读取成功批次、解析通过且双哈希校验通过的已有响应，不执行网络请求。范围取调用范围与原成功批次范围交集；不迁移旧采集表。申万按原批次复核分页总数、跨页重复后发布；中证排除休市行和请求边界复制占位；缺值不填补。

每个原批次建立公共回放批次；幂等键包含原批次ID、实际范围、解析及定义版本。复用来源记录以 `reused_source_id` 指向原来源，共用文件路径和双哈希，保持原参数及真实取得时间，不重复复制文件或冒充新请求。事实复合外键保证来源属于本批次。异常保留元数据和已发布月份；恢复跳过相同范围已验证完成的回放批次。成功回放只证明已有归档入库，覆盖状态保持unknown／empty。

### 后续采集与读取

现有中证历史、申万分析及乐咕PE/PB采集，在同一个估值响应上同步解析点位，不增加行情请求。申万全部页验证通过后，估值和点位按月在同一事务发布；点位失败回滚当月的两类事实。沿用原采集的限速、取消、恢复和拒绝冷却。打开页面／启动后台不自动回放，历史回放需明确启动。

未来增量仍按现有估值采集水位推进，写入当次点位；旧归档缺写由回放补充，不为点位全量重抓。不含点位的估值来源不自动请求其他行情接口。未来独立行情适配器须接入公共调度，按点位序列自身水位补尾部，核定字段和收益口径后登记定义。

`read_prices`明确选择指数、提供方、序列、频率、收益口径、定义版本及日期范围，本地读取，不拼接来源。现有指数分析页已可叠加本地行情点位；没有另建行情API。点位事实的insert/update/delete触发`index_read_revisions`，与估值、目录及定义变更一起令服务端和浏览器图表缓存失效。

<a id="point-maintenance"></a>
### 维护与验证

2026-10-12已在正式备份后持有数据库实例锁，事务内重建点位事实的两个现有唯一索引，未改变表或索引定义。索引物理页占用由25366528字节降至15482880字节，释放9883648字节（约9.43 MiB）供库内复用；未执行VACUUM，数据库文件大小不因此缩小。352335条点位及347619条估值的全行SHA256前后一致，完整性／外键检查和8765页面／接口核验通过。报告为 `data/verification/reports/2026-10-12-index-price-reindex.json`，维护统计为同目录 `2026-10-12-index-price-reindex-maintenance-api.json`。

v27最初新增的三个点位表归入「指数数据」。定义表按 `updated_at`，序列表按 `created_at`，事实表按解码后的 `updated_at` 和关联来源的 `obtained_at`；空表显示暂无数据，缺时间不使用交易日替代。v37扩展行情字段，v38重建为紧凑存储及只读逻辑视图；均不改写旧估值、财务或个股价格事实。

隔离库验证迁移、修订、空值撤回、类别兼容、分页原子性、月频快照、零网络回放、幂等和维护统计。正式迁移前由已有机制备份SQLite；资料迁移须同时复制数据库旁 `collection_sources/` 并校验哈希。验收资料在 `data/verification/reports/`，临时测试副本完成后清理，正式备份和来源保留。

只读验收命令：

```powershell
python -m quarterly_dashboard.index_price_audit --report data/verification/reports/index-points-audit.json
```

核对原始文件双哈希、逐点位值及真实取得时间；相同重叠来源允许复用已保留的相同业务版本。审计同时输出按类别／提供方／频率的范围与逐字段有效数、外键和完整性、维护页新表统计。零网络请求。2026-10-11隔离真实归档演练共回放298个原批次、352335条点位，二次回放全部跳过；原估值内容哈希未变。详细验收在 `data/verification/reports/2026-10-11-index-points-rehearsal-*.json`。

2026-10-11日常库已备份迁移并离线导入同一批352335条最新事实：宽基47820条／40序列，行业301603条／162序列，策略2912条／5序列。数量包含不同提供方、接口及日／月频序列，不代表独立交易日或全窗口覆盖。正式逐来源值／时间／双哈希核对、外键与完整性通过，原估值全内容SHA256保持一致；8765后台已重启到v27，主页／指数页／目录接口正常。正式报告为 `2026-10-11-index-points-daily-replay.json`、`2026-10-11-index-points-daily-audit.json`、`2026-10-11-index-points-daily-summary.json`，均在 `data/verification/reports/`；日常日志为 `data/verification/logs/daily-8765-index-points.out.log` 和 `.err.log`。

## 3. 估值字段、来源与采集

<a id="指数估值采集与查询"></a>

### 指数分类来源总表

下表按业务分类汇总当前已确认的来源映射。宽基、策略逐指数列出；行业指数按申万总体来源归纳。这里的“来源”指项目已登记并有入库事实的提供方／接口序列，不代表每个来源在全部历史日期、全部字段上均有值。频率、估值口径和来源字段定义见后续字段映射表。

| 分类／指数（代码） | 点数来源 | PE 来源 | PB 来源 | 股息率来源 |
|---|---|---|---|---|
| 宽基：上证指数（000001） | 中证日频 | 中证滚动PE；中证看板近期静态PE | 中证看板近期PB | 中证看板近期股息率 |
| 宽基：上证50（000016） | 中证日频；乐咕月频 | 中证滚动PE；中证看板近期静态PE；乐咕月频PE | 中证看板近期PB；乐咕月频PB | 中证看板近期股息率 |
| 宽基：上证180（000010） | 中证日频；乐咕月频 | 中证滚动PE；中证看板近期静态PE；乐咕月频PE | 中证看板近期PB；乐咕月频PB | 中证看板近期股息率 |
| 宽基：上证380（000009） | 中证日频；乐咕月频 | 中证滚动PE；中证看板近期静态PE；乐咕月频PE | 中证看板近期PB；乐咕月频PB | 中证看板近期股息率 |
| 宽基：沪深300（000300） | 中证日频；乐咕月频 | 中证滚动PE；中证看板近期静态PE；乐咕月频PE；中证近期XLS来源披露PE（总股本／计算股本口径） | 中证看板近期PB；乐咕月频PB | 中证看板近期股息率；中证近期XLS（总股本／计算股本口径） |
| 宽基：中证A100（000903） | 中证日频；乐咕月频 | 中证滚动PE；乐咕月频PE | 乐咕月频PB | — |
| 宽基：中证500（000905） | 中证日频；乐咕月频 | 中证滚动PE；中证看板近期静态PE；乐咕月频PE | 中证看板近期PB；乐咕月频PB | 中证看板近期股息率 |
| 宽基：中证800（000906） | 中证日频；乐咕月频 | 中证滚动PE；乐咕月频PE | 乐咕月频PB | — |
| 宽基：中证1000（000852） | 中证日频；乐咕月频 | 中证滚动PE；乐咕月频PE | 乐咕月频PB | — |
| 宽基：中证全指（000985） | 中证日频 | 中证滚动PE | — | — |
| 宽基：中证A500（000510） | 中证日频 | 中证滚动PE | — | — |
| 宽基：中证A50（930050） | 中证日频 | 中证滚动PE | — | — |
| 宽基：中证2000（932000） | 中证日频 | 中证滚动PE | — | — |
| 宽基：科创50（000688） | 中证日频 | 中证滚动PE | — | — |
| 宽基：科创综指（000680） | 中证日频 | 中证滚动PE | — | — |
| 宽基：深证100（399330） | 新浪日频点数；乐咕月频 | 乐咕月频PE | 乐咕月频PB | — |
| 宽基：创业板50（399673） | 新浪日频点数；乐咕月频 | 乐咕月频PE | 乐咕月频PB | — |
| 策略：上证红利（000015） | 中证日频；乐咕月频 | 中证滚动PE；中证看板近期静态PE；乐咕月频PE | 中证看板近期PB；乐咕月频PB | 中证看板近期股息率 |
| 策略：中证红利（000922） | — | 中证看板近期静态PE、滚动PE | 中证看板近期PB | 中证看板近期股息率 |
| 策略：深证红利（399324） | 乐咕月频 | 乐咕月频PE | 乐咕月频PB | — |
| 行业：申万一级、二级（总体） | 申万 `index_analysis_report.closeindex`，日频 | 申万 `pe`，按来源披露PE保存（日频；利润期间待核实） | 申万 `pb`，日频 | 申万 `dp`，日频、百分数口径 |

注：乐咕PE/PB为月频移动观察，普通、等权、中位数三种统计方法分别保留；中证看板／XLS属于近期或快照来源，不能据此宣称有长期完整序列。表中“—”表示当前没有已确认并入库的对应来源，不代表指数本身不存在该指标。点数来源也按真实序列分别保存，不跨提供方或频率拼接。

### 估值字段映射、单位和缺值

| 标准字段 | 目标表列 | 公共batch dataset／定义来源与原字段 | 单位和统计口径 | 频率 | 缺值与存储 |
|---|---|---|---|---|---|
| `pe_source` | `index_valuation_observations.pe_source` | 中证公共dataset `index.cs.indicator` / definition dataset `indicator-xls` (`pe1/pe2`，总股本/计算股本两序列)；申万 `index.sw.analysis` / definition dataset `index_analysis_report.pe` | 倍数；申万利润期间待核实 | 日 | 空值/破折号不造值，来源缺值状态保留 |
| `pe_dynamic` | `index_valuation_observations.pe_dynamic` | 当前无已确认dataset/raw field | 倍数；不把静态或来源不明字段改名为动态PE | — | 无来源值，保持NULL |
| `pe_static` | `index_valuation_observations.pe_static` | 中证 `index.cs.dashboard` / `indexValuation.pe`；Legu `index.legu.pe` / `index-basic-pe` (`addLyrPe/lyrPe/middleLyrPe`) | 倍数；Legu保留LYR标签和统计方法 | 中证日频；Legu月频 | provider及序列分开，来源缺值留NULL |
| `pe_ttm` | `index_valuation_observations.pe_ttm` | Legu `index.legu.pe` / `index-basic-pe` (`addTtmPe/ttmPe/middleTtmPe`) | 倍数，原TTM字段 | 月 | 三种统计方法独立序列 |
| `pe_rolling` | `index_valuation_observations.pe_rolling` | 中证公共dataset `index.cs.history` / definition dataset `index-perf.peg`；`index.cs.dashboard` / `indexValuation.peg` | 倍数；滚动利润期间 | 日 | 复制边界占位按回放规则剔除，真实来源缺值保留 |
| `pb` | `index_valuation_observations.pb` | 中证 `index.cs.dashboard` / `indexValuation.pb`；申万 `index.sw.analysis` / `index_analysis_report.pb`；Legu `index.legu.pb` / `index-basic-pb` (`addPb/pb/middlePb`) | 倍数；Legu统计方法分开 | 中证/申万日频；Legu月频 | 来源序列分开，缺值留NULL，不跨序列合并 |
| `ps` | `index_valuation_observations.ps` | 当前无已确认dataset/raw field | 倍数，不推导替代 | — | 无来源值，保持NULL |
| `ps_ttm` | `index_valuation_observations.ps_ttm` | 当前无已确认dataset/raw field | 倍数，不推导替代 | — | 无来源值，保持NULL |
| `dividend_yield` | `index_valuation_observations.dividend_yield` | 中证 `index.cs.dashboard` / `indexValuation.dp`；`index.cs.indicator` / `indicator-xls.dp1/dp2`；申万 `index.sw.analysis` / `index_analysis_report.dp` | 百分数，2.5即2.5%；中证dp1/dp2分别总股本/计算股本 | 中证/申万日频 | 口径序列分开，来源空值留NULL |

估值序列身份为指数×提供方×序列×频率×定义版本，具体口径差异（利润期间、统计方法、股本口径）保存在定义正文中。只有定义明确支持的字段才进入该序列；有效零/负值按定义保留，不能用相似名称字段填补`pe_dynamic`、`ps`或`ps_ttm`。定义文件 `quarterly_dashboard/index_definitions.py` 是当前映射的实现依据。

指数分析页另有独立[大盘拥挤度区块](#大盘拥挤度采集与查询)：首次API历史+HTML近期，日更仅HTML、只写数据库；复用本页operation与公共legu预算，默认保守策略，不套指数估值专属提速。原估值按钮省略datasets时仍执行原任务，不自动附加该指标。图中所选宽基点位只读现有点位库。

从v27起，中证历史、申万分析和乐咕PE/PB响应中的点位同步写入独立点位表，不增加来源请求；已归档响应也可手动离线回放并保留原采集时间及来源。v37扩展中证与申万的点位定义，新增字段映射见[行情字段表](#field-mapping)；乐咕继续保留月频；回放方案见[指数点位存储](#指数点位存储与归档复用)。

实现入口 `quarterly_dashboard/index_collection.py`，迁移 v23/v24/v25/v26。日常后台初始化先备份再迁移；不修改旧财务／价格采集元数据、文件或事实外键。主页“指数分析”打开 `/indices`，进入页面只读本地数据；采集按钮才请求来源。

### 来源与口径

- 中证：已确认九个指数十年日频滚动 PE；最新板块八指数静态 PE、滚动 PE、PB、股息率；已验证沪深300近期 XLS 两种股本口径 PE／股息率。最新／近期接口不能承担十年 PB／股息率回填。
- 乐咕：十二指数月频 PE／PB，普通、等权、中位数及两个接口分开。红利分类为策略。未结束月份的移动观察点保留原日期与历史快照，查询每月展示最新实际观察点。
- 申万：一级、二级日报，PE期间待核对存 `pe_source`，PB及百分数股息率。按月分段、500条分页，最新月份优先。容量已用真实响应验证（一级两日62条、二级两日262条、二级月度首页面500／总2751条）；全部页数／总数／重复检查通过才发布该月。历史应有名单未核实，覆盖率未知。二级父级未核实保持待补，不猜代码或复用公司分类身份。
- 本次不生成主题指数请求，`theme` 只以隔离合成样例验证扩展性。全量在已确认指数及最新／近期来源之后复查十三个中证候选；取得有效响应才入目录，候选覆盖仍待核实，不把403判为不存在，遇拒绝立即结束同源队列。

上线实采已确认上证指数／上证红利在十年窗口各有2428个有效滚动PE；A500、A50、中证2000、科创50、科创综指的有效PE分别从2024-09-03、2023-12-28、2023-06-13、2020-07-06、2024-10-23开始。这是本次窗口内有效值范围，不是全历史最早日期或正式指数有效期。新增有值候选进入日常增量名单。`000680` 官网实际为科创综指，初候选误标的科创100已纠正；六个深市代码本轮中证历史接口返回空列表，不能据此推断指数不存在。

### 存储与定义

`collection_batches` 按数据集／来源／范围登记生命周期，以 `operation_id` 关联操作，执行与覆盖状态分开。`collection_sources` 逐尝试保存成功／失败及解析状态；HTTP实体先gzip原子落盘再登记，原文／存储双哈希分开。路径相对数据库目录：`collection_sources/<提供方>/<数据集>/...`。无响应及历史未知信息允许空。

`index_identities` 提供稳定 `cn:`／`sw:` 身份；`index_catalog` 保存名称、类别、体系、层级、父级、正式有效期版本；`index_provider_codes` 保存不重叠提供方映射。未知有效期记录只用于导航，历史查询显示待核对；已确认版本按观察日匹配。父级验证同体系、相邻层级、有效期相容、无环。首次观测日不充当正式有效期。当前采集尚缺正式历史目录及二级父级证据，保持待补。

`index_valuation_series` 唯一保存指数×提供方×稳定序列×频率×定义版本，事实通过整数 `series_id` 引用；不同提供方、接口、频率和定义版本不混合。`index_valuation_observations` 的历史唯一键为序列ID×观察日期×修订版本，`index_valuation_latest` 部分唯一索引保证每个序列日期只有一个最新版本；不再保留旧文本组合日期索引。写入按序列ID查询修订，历史读取先解析序列身份，再按整数ID及日期读取；月度去重按相同序列ID取当月最新实际观察日。覆盖和API仍返回原来的指数、提供方、口径、频率、定义版本，不暴露内部ID。

`index_metric_definitions` 保存 `index_definitions.py` 的字段映射、统计方法、利润期间、股本口径、倍数／百分数、空值规则及证据。申万未知 PE 不映射静态／TTM，PE1/PE2不当成动态／静态。股息率2.5存为2.5%，有效零／负值保留。

组合外键确保事实来源属于批次。指标及定义未变化不增事实版本，仍保存新来源；真实修订新增版本、旧版本保留。缺字段或不可解析数值失败；有效值变空须核实撤回原因，禁止静默覆盖。查询明确选择提供方、序列、定义版本；周频仅从日频取每周最后已采交易日，不给月频插值。

`index-v2` 解析还识别中证把首个基日记录复制到请求边界的情况：起日为真实交易日也不能凭此入库，需同时核实PE为空、后续日期有长间隔且其他来源字段完全复制。已核实科创50／综指的2016-10-11复制行剔除，原文与修复审计保留。真实日期上来源明确缺失的PE仍留空。查询逐指标报告有效值起止日和个数，分别列出来源观测数，不用行情起点冒充PE历史起点。

`collection_provider_state` 保存共享预算和冷却，`index_collection_operations` 保存进度／取消。新表已登记维护页；更新时间用真实 `updated_at/obtained_at/started_at/finished_at`，不使用观察日期；缺时间显示未记录，空表暂无数据。

### 采集、限速及恢复

用户授权的指数提速策略：跨提供方并行；中证、申万同源最多2个真实HTTP请求在途。2026-10-16用户因Legu出现429将该来源降为单并发，Legu的指数PE/PB工作项串行执行，公共调度独占同源OS槽位，跨线程及进程最多1个请求。2026-10-17用户将指数Legu请求完成后的等待调整为3—7秒（3秒加0—4秒随机等待），中证和申万仍为2—5秒；滚动60秒最多30次；每完成20次额外休息10秒。所有接口、分页及重试仍共用公共 `collection_provider_state` 预算和跨进程OS槽位；普通采集及SDK调用独占同源槽位，与指数请求互斥。每个工作单元使用独立Session；日历准备完成后再调度。申万按一级、二级各自由最新月份向旧月份串行推进，两级可并行；分页在月份×层级任务内串行，完整校验后才发布。拒绝立即停止本轮该来源的新请求，在途响应归档后持久化至少1小时冷却；其他来源可继续。单并发不保证不再限流。变更见[Legu单并发计划](plans/20261016_Legu采集单并发/PLAN.md)。

指数以外来源仍采用公共默认策略：同源并发1；请求完成后至少10秒＋0—5秒随机延迟；滚动60秒最多5次；每20次额外休息60—120秒。巨潮 `cninfo` 保持独立的2秒、30/60秒、每20次无额外休息策略。连接错误／超时／暂时5xx最多3次，重试至少60／120秒后仍遵守对应预算。等待可取消，不持有SQLite写事务。

403／429、验证码／反爬HTML立即停止本轮全部同源请求，归档响应及冷却至少1小时，遵守更长 `Retry-After`，反复拒绝延长冷却。后续任务到期后只发一次恢复验证，有效才续采；本轮不会自行恢复。其他来源可独立继续，无IP／代理轮换或验证码绕过。

全量默认上海运行日减十年（闰日取目标年2月末），截止不晚于上一个已完成交易日；来源待发布日期报告缺口。中证响应按月短事务发布，申万每月完整分页后发布。再次全量跳过同范围、代码／层级已完成且无明确缺口批次，失败分段重取，事实幂等。部分发布后异常保留已提交月份，记录失败／部分。

### 手动更新与修订核对

页面操作覆盖所有配置来源，不受当前选中的指数／行业影响；没有定时自动采集，打开页面及重启后台不请求估值来源。

| 操作 | 实际范围与请求规则 |
|---|---|
| 更新近期估值（incremental） | 先只读本地水位，仅补最后已有日期之后的新交易日；日期已到截止日时跳过对应估值接口。北京时间15:00后，若当天是交易日且未指定更早截止日，截止日扩至当天，中证、乐咕和申万均按各自来源实际返回更新；来源没有当天观察时不补造数据。中证历史按指数序列、看板按八指数、近期文件按两种股本序列分别判断。申万一级／二级独立判断和请求，成功批次才可证明完成，单个停用行业不拖回多年。没有既有数据的序列使用最近10交易日窗口，不自动启动十年建档。 |
| 核对近期修订（review） | 主动重抓日频最近10个已完成交易日；乐咕重抓截止月份及之前3个月，不因本地日期已最新而跳过。用于发现来源数值修订；未变化事实保留原出处，变化新增版本。 |
| 采集／恢复十年历史（full） | 滚动十年窗口，按相同范围／口径检查完成分段并恢复；未知覆盖的成功分段可跳过，不等于已补齐来源历史缺日。 |
| 轮换核对历史月份（audit） | 每次核对一个较早月份，排除最近4个月，按成功核对任务次数轮换；用于历史修订和缺口复查。 |
| 停止采集 | 安全取消等待／后续请求，已发出的HTTP请求等待返回或超时；已提交分段与来源保留。 |

北京时间15:00前以及近期修订核对仍以当天之前最近交易日为截止日；增量更新在15:00后可将当天纳入截止日，且仅在交易日历确认当天为交易日时生效。常规更新与近期核对优先复用哈希验证通过、日期范围覆盖运行日的本地交易日历；缺少或过期时才获取日历。日期已最新判断来自本地日历和来源序列，不能保证数值未被来源修订、来源当天已发布，或历史内部没有缺行。

中证和申万新数据从最后日期后的第一个交易日起采，久未运行会自然补齐尾部空档，不再每次重复最近10天。申万层级只以完成批次内数据判断，失败／取消分段不能冒充完整；仍不推断每个行业的正式历史有效期。内部历史缺口另做核对，不因“日期已最新”改为完整覆盖。

乐咕按指数PE／PB各三种统计序列的较早最新日期判断；当前月移动观察日早于截止日时，需要重新取得该月份，保留真实观察日期。该接口不支持服务端日期过滤，确需更新时仍下载完整接口响应，再按月份筛选发布；仅在本地判断已最新时省去整个请求。历史月频端点有发布日期差异，未覆盖截止日不承诺一次更新即最新。中证看板／近期XLS也只发布接口实际可返回日期，不伪造缺失历史。

显式指定CLI/API start时按指定范围采集，不应用本地最新跳过规则，适合定向复查。即使全部跳过，仍保存一次operation及最终状态；不会新增估值请求来源、原始响应或事实版本（需要更新交易日历时，其来源记录独立保留）。

历史轮换每次核对一个较早月份；季度盘点读本地范围与批次汇总，需要全窗口重查可显式指定范围。这些是手动入口，未创建定时任务。

```powershell
python -m quarterly_dashboard.index_collection --mode full
python -m quarterly_dashboard.index_collection --mode incremental
python -m quarterly_dashboard.index_collection --mode review
python -m quarterly_dashboard.index_collection --mode audit --start 2018-06-01 --end 2018-06-30
```

后台先初始化最新结构，CLI不在运行后台旁迁移schema。页面可查看进度／停止；重启只恢复没有活跃操作锁的中断任务，不自动请求来源。

`index_probe_import` 可导入哈希核验的前期指数响应，保留原参数、原采集时间和样本引用，不冒充新请求或旧采集迁移。只覆盖指定证据窗口，不宣称后续日期已更新。

SQLite备份不包含原始响应。换电脑须同时复制数据库旁的 `collection_sources/` 并验证哈希，才有完整追溯。

<a id="valuation-verification"></a>
### 验证

2026-10-15指数提速采用Sol规划/审查、Luna实现的协作流程，计划见[本轮计划](plans/20261015_指数采集提速/PLAN.md)。相关9个测试文件最终128项通过；新增 `tests/test_index_parallel_collection.py` 与 `tests/test_index_parallel_review.py` 覆盖实际并发2、跨进程上限与尝试号唯一、跨源重叠、等待/窗口/休息、旧预算兼容、取消收尾、恢复探测、失败统计和申万月份顺序。页面采集中显示各来源进度。

有限实采工具为 `python -m data.verification.tools.index_parallel_probe --date <已完成交易日> --report <报告路径>`：复用日常库预算和原文归档，每源最多两个数据请求，乐咕另一次初始化，不重试、不发布估值或点位事实。此次7次请求全成功，三源各达到同源并发2，全局峰值4，7份响应双哈希校验通过；这是有限样本验证，不代表完整更新的耗时基准。[验收记录](../data/verification/reports/index-parallel-acceptance-20261015.md)和[实采时序](../data/verification/reports/index-parallel-live-20261015.json)保留在本机验证目录。

`tests/test_index_collection.py` 用隔离数据库／假时钟验证限速、重试、冷却、恢复、取消、修订／撤回、来源外键、目录分类／有效期和截断分页；`tests/test_index_parallel_collection.py` 验证中证同源并发达到2、跨来源重叠，以及Legu工作项和独立采集器的实际HTTP串行与完成间隔。旧财务一批多数据集、价格去重／名单引用、同号ID区分、未知信息、不同哈希对象用隔离公共表演练，不迁移旧记录。真实样本回放报告在 `data/verification/reports/`，样本在 `samples/`。

<a id="indices-page"></a>
### 指数分析页面

独立入口 `/indices`，主页导航为「指数分析」，采集进度保留在估值区之前。类别分宽基、行业、策略；行业采用一级行业、二级行业两个筛选，二级默认「全部二级行业」并展示一级指数自身估值，不汇总二级估值。选择具体二级才展示该指数估值；切换一级重置二级默认值，只列对应子行业。宽基及策略保留指数选择。

首次进入默认沪深300；选过指数后，本标签页会话记忆指数类别与代码，切换页面返回或刷新时恢复。行业选择按当前目录还原一级、二级关系。目录中已失效的代码或损坏的记忆回退默认选择；记忆不触发采集。

行业内公司可反向进入 `/indices?stock=...&index=...#valuation-panel`：一级／二级对应本级指数，三级使用父二级。一次性 index 参数优先于记忆，目录校验恢复后移除该参数，避免后续刷新覆盖新选择；stock 和估值区锚点保留。

行业筛选右侧提供纯文字「前往行业内公司」入口（无图标），通过当前 SW2021 导航分类映射打开行业对比，选中对应一级或二级行业、激活行业内公司标签并定位到列表。一级默认显示全部下属公司；未匹配分类时按钮禁用，非行业类别隐藏。跳转保留当前股票上下文，只读取本地数据。个股估值区提供同样式入口，明确点击后跳至该股所属三级行业；行业页直接打开默认全市场，选择记忆见行业试运行说明。

当前导航复用 `sw_industries` 的 SW2021 父子关系：指数代码与分类代码不同，先按精确名称＋相同层级唯一匹配，再以分类父级找到唯一对应的一级指数。当前31个一级、131个二级均可匹配。只对申万体系及 `sw:` 指数应用；名称歧义、父级不存在、体系不一致保持未映射，不猜代码。不改写 `index_catalog` 的历史身份、有效期及父级记录。API以 `navigation_taxonomy_code/navigation_parent_index_code/navigation_mapping_source` 返回当前筛选映射，历史 `catalog_as_of` 仍只读已确认历史版本。读本地关系表，无新增网络采集。

「估值与分位」使用个股同一 `valuation_summary_observations`：最近3/5/10年；5年按周最后有效值取样，3/10年在完整交易日覆盖未核实时按月最后有效值取样；当前值为区间最后有效观察。分位为严格小于当前值的样本比例；80/50/20分位线性插值，标准差为总体标准差，均值减标准差不截断。卡片按5/20/40/60/80/95百分位划档，股息率颜色反向，改变日/周/月展示或比较基准不改变分位判断。PE/PB/PS正值参与，股息率允许有效零，负PE原值保留并标示区间；来源披露PE未推定为TTM。

来源、序列及定义版本独立选择，不拼接不同口径；月频来源仅可按月展示，原始观察及来源保存在数据库/API。点位叠加可选本地有数据的单一来源序列；无数据时保持不可用，不使用个股前复权价格或编造指数价格。指数不套用个股同行业平均，按自身历史判断。

指数页面沿用主页侧边栏导航样式，当前指数名称随选择更新；从主页进入时保留个股代码供返回各研究页使用。折线图采用横轴统一悬浮提示，标题显示完整日期（`YYYY-MM-DD`），各序列仅显示指标名称、数值和单位，不重复列出观察日期。

### 所选指数／行业拥挤度

「估值与分位」新增拥挤度切换，随宽基、行业（一／二级）或策略指数选择联动。只读 `index_price_observations.amount_ratio`（百分数，4.82 即 4.82%），选择有该字段的单一日频来源，行业优先申万、宽基优先中证；不拼接来源、频率或收益口径。查询入口 `/api/indices/crowding?code=...` 复用指数读取版本和有界缓存，不采集、不新增事实表。

该拥挤度为所选指数或行业成交额÷市场总成交额×100%，区别于下方大盘「前5%个股集中度」。中证宽基值为库中既有估算（指数成交额÷同日全A成交额×100%，Legu优先、授权20日缺值回退BigQuant），申万为 `bargainsumrate` 原值。无值留空、有效零保留。3/5/10年分位均按全部有效日样本，使用大盘同一统计函数，低于当前值的比例、80/50/20线性插值分位及总体标准差；频率切换不改变统计。自动展示时3年和5年按日、10年按周；周／月取最后已记录日，最后日缺值不以前值补齐。可叠加同一来源序列的真实收盘点位，显示百分比卡片、分档、参考线与双轴走势。无可用序列显示空态。

### 指标优先选择

宽基先选 PE(TTM)/PB；行业先选 PE(TTM)/PB/股息率。宽基 PE 默认中证 `history.rolling` 的 `pe_rolling`，PB 默认乐咕 `pb.ordinary` 的 `pb`（月度）。估值口径选择放在指标旁，只列定义包含当前字段且有有效观察的候选。PE 仅使用 `pe_ttm`/`pe_rolling`/来源披露 `pe_source`，不以静态或动态PE替代TTM；PB 只用 `pb`。首选缺少有效值时从当前指标可用候选选择，显示真实提供方及序列；完全缺失时留空，不切换指标冒充。

行业仅选申万 `analysis.daily`，页面显示固定来源、不显示口径下拉。行业 PE(TTM) 按用户指定保留指标入口名称，但底层仍读 `pe_source`，在卡片旁明确提示「利润期间尚待核实，不能视为已确认的TTM」，不改来源定义或历史数据。策略保留 PE/PB/股息率入口及按当前指标过滤的来源选择。

指数及一/二级行业选择项仅显示名称，代码仍用于内部查询。宽基初始默认沪深300，切回宽基且当前选择不适用时也默认沪深300；用户已选的宽基在目录刷新时保留。

### 整数序列迁移

v25迁移前由现有初始化流程备份，事务内构建序列表和新事实表，逐字段EXCEPT及记录数检查通过后替换旧表。观察ID、金额、哈希、修订版本、最新标记、来源、批次、取得及写入时间保持原值，不新增业务修订。来源/批次组合外键保留，提供方/口径/定义外键移入序列表；序列身份禁止原地更新。序列表 `created_at` 记录此次真实建档时间，不拿观察日期或早期来源时间冒充新增表写入时间；空表显示暂无数据。

结构迁移仅释放可复用页，不自动缩小文件。此次部署另在备份及校验后停服持锁整理空间；日常启动仍不自动VACUUM。旧研究结果见[研究报告](../data/verification/reports/2026-10-11-index-storage-research.md)，研究脚本针对v24备份；v25离线迁移及验收工具为 `python -m data.verification.tools.index_series_upgrade --db <v24数据库> --report <报告路径> --compact`，运行前必须停止同库后台。

### 指数读取与有界缓存

页面使用三个只读接口：`/api/indices/catalog` 返回精简目录、行业关系、覆盖统计及定义；`/api/indices/status` 只查询最近采集状态及冷却状态；`/api/indices/chart` 按指数、提供方、序列、定义版本及实际指标读取历史。旧 `/api/indices` 保留原始记录及来源查询兼容，页面不再用它加载图表。

图表接口沿用原有计算入口，仅计算所选指标的3/5/10年视图，返回参考线、分位、负PE区间以及按日/周/月显示的日期、数值和实际观察日；省去原始审计字段及重复的默认频率行。切换时间范围、比较基准和显示频率在页面完成；切换指标按实际字段区分缓存，不混合提供方和口径。月频来源仍不派生日/周频，股息率零值及负PE处理不变。

后台缓存编码后的JSON字节，按LRU最多16份且合计不超过32 MiB，超过单份容量上限不缓存。目录仅保留当前版本一份；并发冷请求串行计算，相同请求复用已有结果，避免同时分配多份计算内存。该容量仅约束保留的图表响应，不包含目录、临时计算、SQLite及Python进程本身。

v26新增 `index_read_revisions` 单行版本记录；v28增加`index_price_observations`的INSERT/UPDATE/DELETE触发器。当前估值和点位事实/序列、目录、指标定义及SW行业关系变更均在同一事务内递增版本。持久只读连接读取已提交版本，变化时使目录及图表缓存失效；个股最近访问、筛选偏好、其他财务写入、采集进度及冷却状态不使指数缓存失效。相同估值重复发布不新增版本；失败回滚不改变版本。历史修订、原地修改及删除均能检测，不依赖最新观察日或最大记录ID判断。日期变化也使缓存失效，保持滚动窗口正确。后台停止或数据库恢复前关闭只读连接并释放缓存；恢复后的新服务使用独立版本标识，防止同路径旧缓存复用。

浏览器仅缓存图表JSON字符串，LRU最多6份且按UTF-8计量不超过8 MiB；当前绘图对象、Plotly及JavaScript字符串存储另有内存开销，不能把该上限当作页面总内存。页面连续切换会中止上次fetch并忽略过时响应。采集中每5秒只查询轻量status，更新进度文字/提供方计数/按钮；即使数据版本变化，也暂不更新目录或图表。结束后核对版本，变化才刷新目录并清除缓存、读取当前图表。空闲及隐藏页面不定时查询；返回或聚焦页面可检查一次版本。图表使用与指数/序列/指标/范围/频率绑定的uirevision，数据更新保留用户缩放；主动改变相应筛选重置。前端总体检查见[页面刷新说明](frontend-refresh-behavior.md)。

指数任务的“查看进度”默认收起，展开后显示总体及各来源的独立采集工作项计数。中证按指数历史、板块看板及近期估值文件计项；乐咕按指数和PE/PB计项；申万按月份与行业级别计项，分页、重试和cookie初始化不重复计数。成功数只在该工作项校验并发布后增加；已最新或已有完整分段单独记为跳过，失败、停止和待执行分别显示。计数用于描述本轮任务执行状态，不代表事实数据行数、来源历史完整度或覆盖率；旧操作未记录该结构化进度时明确显示未知。

只读基准工具：`python -m data.verification.tools.index_read_benchmark --report data/verification/reports/<报告名>.json`。测量包括JSON编码但不含HTTP及绘图，Python分配追踪不代表总进程RSS。

版本表的 `updated_at` 为真实初始化或相关写入时间，维护页登记为指数数据及缓存版本用途；迁移不重写估值事实，仅建立单行版本和15个触发器，初始化流程仍先备份。版本只作失效标识，数值差不代表新增观察条数。

## 4. 行情依赖与派生调度

指数操作先确定本轮实际采集截止日（审计模式先完成月份轮换），再在同一operation和`index-operation`互斥锁内检查该交易日的大盘总成交额。仅有限且大于0的Legu `total_amount_yi`有效；若本地已有效则不发请求。缺失时，仅对已初始化、启用且非冷却状态执行一次现有HTML-only检查，复用`RequestCollector`、共享Legu预算与锁；不嵌套启动operation、不初始化API、不轮询等待。暂停、冷却、拒绝、解析失败或网页尚未含目标日期时，保持缺额并继续CSI/SWS原始采集。HTML更新只保存metadata_only请求记录和结构化事实。

依赖检查按上海本地日/阈值阶段复用：18:00前前置检查不阻止同日18:00后例行检查；18:00后已完成检查可供后续指数operation复用，即使总额仍缺。有效目标日总额可跨手动、定时和前置检查复用。单日HTML窗口之外的历史缺额保持待补，不抓API回填。依赖batch状态单独写入operation结果，不计入原始行情失败批次数。

中证/申万行情先按来源校验发布，再于同一operation内补算同行字段；HTML更新后对其返回日期范围补算，指数常规采集仅对本轮日期范围补算。历史首次扩展需显式回放已有归档和补算，不混入每次常规采集。双方先后到齐均可补算；输入修订或失效后就地更新/撤回，未变化幂等。缺分母或输入时保留NULL并报告待补日期，不把原始采集误报失败。按日期短事务提交、支持取消，不增加行情请求或调度器。

## 5. 拥挤度来源、日程与图表

<a id="大盘拥挤度采集与查询"></a>

实现：`quarterly_dashboard/market_congestion.py`、迁移v36。指数分析页 `/indices` 在估值区块下方提供独立趋势图。

图表“成交额”开关可叠加全A成交额（亿元），使用同日有效值并按网页表格、API、自算补值的顺序取值；日频显示当日金额，周／月频汇总该周期内有效日金额，缺值不补。该线使用独立纵轴，不改变拥挤度、分位或宽基指数点位。

大盘拥挤度图表下方提供申万一级／二级行业热力图，日期范围默认最近30个自然日并可改为60／90日；日期从新到旧排列，行按选中日期的拥挤度降序，前五名置顶。行业拥挤度为行业成交额÷市场总成交额×100%。格内数字和排序均使用同一行业日频 `amount_ratio`；颜色连续映射该行业 `turnover_rate` 在该序列完整有效历史中的分位（严格低于当前值的样本占比），悬浮显示拥挤度、估算行业成交额（亿元）及换手率历史分位。估算行业成交额来自本地 `amount` 字段，按人民币元换算为亿元；申万行业金额由成交额占比乘以同日全A成交额估算，缺失值显示暂无有效值。点击日期标题按该日拥挤度重排，点击行业名切换上方估值与分位到对应拥挤度曲线。接口 `/api/indices/industry-crowding` 只读已有申万 `analysis.points`，不触发采集；换手率历史覆盖仍受本地来源数据覆盖限制。

### 数据与来源

指标是成交额前5%个股成交额/全A成交额，不含大宗交易。`value`为0—1比例，前端显示百分数。`close`为来源给出的收盘点位，基准身份未明确时不猜；`top5_amount_yi`、`total_amount_yi`均为亿元。来源百分数是指标值，金额比例仅核对，不重算替代。零拥挤度及零金额有效，缺值留NULL，不跨日期/通道填充。

| 存储字段 | API dataset `market.legu.congestion` | HTML dataset `market.legu.congestion.page` | 单位/转换、频率和缺值 |
|---|---|---|---|
| `value` | `items[].congestion`（0—1比例）；若只有`congestionPercent`则÷100 | “拥挤度(%)”÷100 | 比例；daily；`value_status='observed/source_missing'`区分有效值与来源缺值；precision为4 |
| `close` | `items[].close` | “收盘价” | 指数点；daily；来源没有基准身份时不映射为某指数 |
| `top5_amount_yi` | `items[].top5AmountYi` | “成交额排名前5%的个股的成交额(亿元)” | 亿元；daily；来源缺失留NULL |
| `total_amount_yi` | `items[].totalAmountYi` | “全部A股成交额(亿元)” | 亿元；daily；来源缺失留NULL，可用于同日行情字段派生估算，但不是申万原始额字段 |

两通道都以`ashares_congestion/all_a/legu/{channel}/daily/v1 + observation_date + version`为事实身份，并在行上保留`value_status`、`precision_digits`、`source_id`、`batch_id`、`obtained_at`和`updated_at`。来源字段有值变空不会静默撤回；batch记录`withheld_dates`，需核实后再发布明确撤回。BigQuant自算身份及来源公式独立，原Legu空值不会被改写。

- `api_history`：首次从Legu API读取全部返回日期，近期待会员权限的日期只有date也保存为空观察。
- `html_recent_table`：同页公开`table#tableID`表格，按表头/单位定位全部行；摘要只核对最新日期及值。每天只采这一通道。
- 两通道独立，来源冲突明确显示。2026-10-17样本API有3662个日期、3612个有效值；HTML30条。Legu有效日期并集3642个，2026-07-31—2026-08-27共20个日期原始缺值保留。条数与表格窗口均是样本观察，不是永久限制或覆盖保证。
- 用户授权用本地BigQuant日行情为这20日增加独立离线补值，provider=bigquant、channel=bigquant_calculated、definition_version=amount_rank_top5_v1。计算采用当日amount非空证券（沪深北），N为有效证券数，取成交额降序前ceil(N/20)只，value=前K成交额/有效成交额总和；零amount保留，空amount排除并记录，空amount但volume不为零时拒绝计算。close取原点数库中证上证日频同日实际值，金额以元除1e8转亿元，比例不提前四舍五入。不改Legu空观察，不当作Legu原值或新外部请求；公式重叠核对与一次性入库资料见[20日离线补值](data-sources/market-congestion-gap-backfill.md)。

### 历史初始化

点击“获取／恢复历史”，复用现有指数operation的互斥、取消、进度与恢复；两个通道分batch，校验完成后短事务发布。优先复用同dataset已归档、请求/解析通过且双哈希正确的来源，保留原取得时间及reused_source_id，不复制文件。可用归档不存在时才联网，HTML与API复用Session/CSRF。API不按年/月拆请求。

完成的通道恢复时跳过。初始化两个通道均有有效观察后，启用每天18:00检查；未初始化时不自动抓API。页面读数据不会初始化或采集。

### 后续每日与手动更新

只取HTML一次，在内存中解析整个表格并更新数据库；不再请求API，无法覆盖的历史日期保持缺口。窗口内每个日期均核对，补新增/空值转有效值、保留真实修订，重复响应不新增事实版本或改原出处。字段有值变空保留旧记录，并在batch结果的withheld_dates登记异常，未经确认不视作撤回。

本指标后续更新是用户授权的`evidence_mode=metadata_only`：公共来源表登记脱敏请求、HTTP/解析状态、真实取得时间，路径/压缩/哈希/字节数为空；不保存HTML、JSON、压缩响应、截图、正文日志、BLOB或逐次报告文件。初次响应及已有归档仍保留。无文件记录不能回放或当原文缓存；原文查询明确返回“仅请求记录，无响应文件”。事实仍有source/batch复合外键与修订，后续只能追溯请求和入库值，不能离线重新解析原HTML。

所有真实来源请求在Windows主机端执行，使用公共RequestCollector，provider=legu；默认保守预算/并发1/拒绝冷却不变，不套指数估值专属提速策略。日更max_attempts=1，失败不在本日自动重试；冷却中当日跳过并留操作结果。

### 后台日程

`collection_schedules`保存开关、Asia/Shanghai时区、HH:MM及真实配置时间。后台生命周期内的调度器每天到点检查，不依赖前端；暂停/改时间通过显式POST。关闭/恢复数据库时停止调度，后台停机期间不能保证准点执行。

日运行日期写入现有operation.scope_json，并由部分唯一索引防止跨进程/重启重复；另有现有operation全局互斥。有其他指数任务时等空闲；当天到点后启动可补运行一次，跨多日停机只取当前一份近期表格。相同日期已实际启动过（包括失败/取消/冷却跳过）不再自动执行。当日到设定时间后已有成功手动检查时，复用检查结果。周末同样允许检查一次，无新增则正常完成，不生成假交易日数据。

<a id="congestion-storage"></a>
### 存储与HTTP

单表`market_indicator_observations`，不建序列表。完整身份为indicator_key、scope_key、provider、channel、frequency、definition_version；Legu是ashares_congestion/all_a/legu/上述通道/daily/v1，自算身份见上文。按身份+日期保留修订，最新版本部分唯一，业务hash包含四项数值与精度/状态。定义登记在代码DEFINITIONS，未知定义不发布；未来新增适配器须明确新指标定义和对象，不直接混写。

事实`obtained_at`来自实际source；`updated_at`为发布/修订时间；每日检查时间来自公共来源。自算source的取得时间是离线计算证据生成时间，原行情取得时间及指数事实ID/version/source/batch保留在归档证据中，不冒称新下载。调度表updated_at是配置时间，不代表数据取得时间。空表及无时间不以观察日期代替。迁移只建表/索引，不自动导入历史；初始化沿用现有升级前备份机制。资料迁移同时复制初始化归档和校验哈希，metadata-only无文件是正常状态。

- `POST /api/indices/collect`，`datasets:["market_congestion"]`，mode=full/incremental/review；增量和核对都HTML-only。
- `POST /api/indices/cancel`复用现有停止入口。
- `GET /api/indices/congestion?code=cn:000001`：纯本地，两条Legu通道及独立自算通道/配套字段、各字段有效数、已知缺值日期/冲突、日程、最近请求及操作结果、宽基目录和点位；supplemented_dates为实际使用自算填空的日期，source_missing_dates保留Legu原始缺值。冲突比较限Legu原始两通道，不将不同定义的自算值当作Legu冲突。可选start/end裁剪观察序列，不裁全局最新。覆盖率未核实时保持unknown。
- `POST /api/indices/congestion/schedule`：enabled布尔值及local_time。未初始化不允许启用。

<a id="congestion-trend"></a>
### 趋势图

比较基准可选历史分位（20%/50%/80%线性插值参考线）或均值±1个总体标准差（除以N），三条线均绑定左侧拥挤度轴。当前判断按5%/20%/40%/60%/80%/95%历史分位分为极低、低拥挤、偏低、适中、偏高、拥挤、极拥挤，颜色沿用估值区块；基准切换只改变参考线，判断仍依据历史分位。

用户明确要求统计判断全部按天：所选5年/10年/全部范围内，每个有效日频日期一个样本，按网页/API/自算的既有优先级去重；有效零计入，缺值不补。历史分位=严格低于当前有效值的日样本数/有效日样本总数，包含当前日在样本中；输入值不提前四舍五入，分位显示一位小数。统计不按周/月抽样，不随曲线展示频率、叠加指数、缩放改变；仅时间范围改变样本。展示最新有效日期的拥挤度及历史位置，不能据此推断后续指数涨跌。

空闲不定时查询。采集中每10秒仅GET轻量`/api/indices/status`，只更新进度文字和按钮，不重新GET完整拥挤度数据、不替换下拉框或重绘图表。任务结束后重新读取当前数据一次。首次打开、主动切换叠加指数及返回可见页面按需读本地数据；同页任务启动/取消通过事件通知两区块更新忙碌状态。稳定uirevision使同筛选条件下的数据更新保留用户缩放/平移，主动切换指数/范围/频率才重置。该前端策略不改变后台每日HTML检查。

默认5年、上证指数，可选5年/10年/全部和本地全部宽基目录（无点位的指数仍可选）。拥挤度左轴百分数、所选宽基右轴实际点位，日期横轴一致；点位取已有库中一个明确来源/频率/收益口径，不新增行情请求。展示层按日期显示一条拥挤度曲线，同日优先网页有效值、空值不覆盖有效值；底层来源仍独立，冲突及来源信息移入默认折叠说明。曲线缺口不补值；图例/下拉/悬浮仅用指数名称，共享悬浮标题只显示一次YYYY-MM-DD日期。可选自动/月/周/日；自动时5年按日展示，10年和全部历史按周展示。周/月按周期最后实际记录日取当日值，当日无值留空，原始日频数据不变。只有月频点位时在说明中标明月频，保留原生观察日和曲线，并在对应周期按同日比较，不补日值。切换范围/指数只读SQLite，与上方估值图互不干扰。日更源close即使有值，也不猜作所选指数价格。

<a id="congestion-verification"></a>
### 验证

`tests/test_market_congestion.py`使用隔离库、模拟时钟/响应及有限浏览器测试；覆盖来源解析、字段/单位/零值、修订/权限空值、无响应文件、HTTP只读/权限、每日防重/冷却/暂停和双轴图。公共预算/取消/恢复和原指数功能沿用相关测试。真实验收资料置于data/verification，不用生产库注入故障。

### 上证指数-legu

按用户明确指定，宽基选择额外提供“上证指数-legu”：读取拥挤度观察表配套close，优先网页有效值、API有效值。用户授权20日补值后，仅这些日期使用独立自算观察中保存的中证上证同日close填空，折叠说明明确来源；不改Legu原记录，不新增行情请求或价格事实。原“上证指数”仍为默认选项。此选项和图例只在拥挤度区块使用，源配套close的原始定义及出处不改。拥挤度曲线同样仅以自算值补空，原始有效值始终优先。

## 6. 读取与图表

<a id="reading-and-charts"></a>

指数行情读取按同一指数、provider、接口序列、频率、收益口径和定义版本选择一个序列；中证/申万优先v2，同一身份无v2序列时退回v1，不按日期区间拼版本。指数分析页叠加行情使用本地已保存点位，不触发来源请求。估值图继续按既有指标优先级选择，PE/PB/股息率位于估值表；拥挤度趋势及日频统计、双轴点位和空闲刷新行为见[趋势图](#congestion-trend)与[指数分析页面](#indices-page)。

## 7. 维护与验收

<a id="maintenance-and-acceptance"></a>

行情v38重建紧凑点位事实，不增加业务表，维护分类仍为“指数数据”，逻辑真实时间为关联来源obtained_at及解码updated_at；新增列、来源、定义版本和派生估算均可从点位事实及source/batch追溯。拥挤度表与日程表的用途、分类和真实时间见[拥挤度存储说明](#congestion-storage)。维护页统计、隔离测试、原始归档双哈希复核及历史点位上线验收记录见[行情维护与历史验收](#point-maintenance)、[估值验证](#valuation-verification)和[拥挤度验证](#congestion-verification)。正式回放定向只读审计使用`python -m quarterly_dashboard.index_price_audit --report data/verification/reports/index-price-targeted.json`：SQL汇总行情行数/物理唯一/来源关联及逐字段覆盖，每序列首中末抽样核验来源双哈希、字段真实时间、同行估算父依赖及公式；成交额/占比覆盖分别给出原始有效与估算有效条数；不重复历史全量逐来源审计、不扫描未变表hash。已有全量审计报告仅作为未变来源/单位的历史证据，新单行模型通过须有本次迁移和幂等验证；发现异常再扩大对应分段。覆盖状态按序列/字段分别核验，任务完成不等于历史全覆盖。

### 2026-10-18 同行补值验收

已备份并直接更新既有320条中证amount_ratio、3240条申万amount，覆盖2026-07-31至08-27共20个交易日，行情行数保持353116；原始字段及市场来源事实哈希前后相同。每条按实际BigQuant分母复算核验，重复补算修改0行，无网络请求、无新增批次或来源。报告见[补算审计](../data/verification/reports/20261018_index_bigquant_gap_recompute.json)，复用工具为`python -m data.verification.tools.index_bigquant_gap_recompute`（会先备份日常库）。
# 中美国债收益率采集

指数页「更新近期估值」同时附加`bond_zh_us_rate`手动工作项，共用`index_collection_operations`进度、取消及公共来源采集器。首次点击先通过Eastmoney首页固定初始化最新观测日，再按2002-01-04至该日倒序逐年请求并校验；已完整年度可跳过，失败年度下次从固定年度第一页恢复。完成初始化后按自身水位补尾并重新读取近期14个日历日。`providers=[]`与`datasets=['bond_zh_us_rate']`可用于单独运行这项验收工作；必须使用`mode='incremental'`且不传日期范围。

来源为Eastmoney `RPTA_WEB_TREASURYYIELD`，使用`SOLAR_DATE`闭区间filter、500行分页、倒序。13列原始映射、13字段单位、来源双哈希和精度规则见[SQLite v39存储说明](sqlite-storage.md#211-中美国债收益率v39)。新增事实保存在`sovereign_yield_observations`，十进制字符串保留来源精度，空值留NULL，零和负值有效；相同值保留旧出处，有值转空暂缓该日期发布并登记待核实候选。每个年度独立短事务发布，取消时保留已完成年度。

国债在组合更新中使用独立工作线程和独立HTTP Session，与中证、乐咕、申万三路并行，从任务开始即可执行，不等待指数日历和衍生计算。国债与指数各自记录进度，任一路失败不阻断其他来源；统一等待各路结束后合并请求数、重试数、冷却状态和最终结果。停止仍读取同一operation取消标记，已提交分段保留，跨进程operation锁覆盖全部分支；注入自定义采集器的测试／工具调用保留确定性串行路径。

Eastmoney标识共享公共`eastmoney`预算，使用并发1、至少10秒间隔加随机等待、滚动60秒最多5次和20次后额外休息60—120秒；它不继承中证/申万/乐咕指数提速策略。中国与美国新鲜度分别按上海、纽约本地日期核对，参考各自昨日并留24小时迟到宽限；有效检查6小时内可复用，字段仍滞后或为空时1小时后重查。GDP按实际非空日期记覆盖，不纳入日度收益率新鲜度。发布时点、字段检查及年度覆盖均在公共batch result中可追溯；任务完成不等于逐字段每日完整。只读覆盖/溯源报告可运行`python -m quarterly_dashboard.bond_yield_audit --database <数据库路径> --output <报告路径>`，核对来源双哈希、分页声明、原文与事实双向值匹配、日期间隔、维护大小/时间及查询/分段写入耗时；日期间隔只报告日历天数，不推断节假日缺行。页面读取不触发网络请求，不自动运行日程。完整实施计划与验收记录见[国债收益率采集计划](plans/20261020_bond_yield_collection/PLAN.md)。


## 股债利差展示

指数分析「估值与分位」在拥挤度旁提供「股债利差」，仅宽基指数可选；指数列表仅包含本地已有 `csindex / history.rolling / v1` 日频 `pe_rolling` 的指数，隐藏其他估值口径。

- 公式：`100 / pe_rolling - china_10y`，页面按用户要求以 `%` 展示，数值不变。PE 来自 `index_valuation_observations`，国债来自 `sovereign_yield_observations` 的 `china_10y`，两者均使用对应 v1 口径的最新修订。
- 按同一观察日匹配，不跨日沿用国债值；PE 非正数或任一项缺失时留空，有效零／负利差保留。以 PE 记录日期为图表时间轴。
- 复用拥挤度的近 3／5／10 年、自动／日／周／月展示、历史分位／均值±总体标准差筛选。统计使用区间全部有效日样本；分位为严格低于当前值的样本比例，参考线为 80%／50%／20% 线性插值分位。频率、叠加点位及缩放不改变分位。
- 周／月展示取周期最后一个已记录 PE 日期，当日利差缺失则留空。叠加指数点位使用同一展示日期的真实收盘值，缺失留空；悬浮框统一显示一次完整日期，数值标明 % 或点。
- 仅查询本地数据，不触发采集或模型。国债新版本发布后，页面重新取得版本并刷新数据。高利差仅表示盈利收益率相对国债较高，不是后续收益预测。

隔离验证：`python -m pytest tests/test_index_spread.py tests/test_index_crowding.py tests/test_index_read_service.py -q`，包括同日匹配、缺值、零／负值、版本刷新、指数筛选、频率切换和真实点位叠加。
