# 指数点位存储与归档复用

结构 v27；实现为 `quarterly_dashboard/index_prices.py`，历史导入为 `quarterly_dashboard/index_price_replay.py`。点位与估值分表，复用 `index_identities / index_catalog / index_provider_codes`，不进入个股价格表。宽基、申万行业、策略和后续主题指数共享结构；分类及层级继续由目录管理。

## 表与口径

| 表 | 内容 | 身份／时间依据 |
|---|---|---|
| `index_price_definitions` | 来源字段、点数单位、频率与缺值规则 | 提供方×接口序列×定义版本；`updated_at`为实际登记时间 |
| `index_price_series` | 指数身份、提供方、接口序列、频率、收益口径和定义版本 | 六字段联合唯一，事实引用整数ID；`created_at`为实际建档时间 |
| `index_price_observations` | 日期、收盘点位及来源有值时的开高低点、修订、批次和来源 | 序列ID×日期×修订；部分唯一索引保证一个最新版本；`obtained_at`为来源真实取得时间，`updated_at`为实际写入时间 |

日频／月频、不同提供方、接口和定义版本不拼接或覆盖。收益口径支持 `price / total_return / net_return / unspecified`；目前 `cn:000680` 科创综指明确为 `total_return`，其余序列保留 `unspecified`，不由类别猜测。以后确认收益口径时建立独立身份，不修改旧序列。[科创综指官方事实表](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000680factsheet.pdf)。

点数只接受有限正数；缺值为NULL，不由收盘点位生成开高低点。校验实际已有的OHLC关系。字段不存在时记录 `price_fields_absent`，不影响原估值解析；字段存在但格式／数值异常时分段失败。暂不标准化成交量／成交额，原文仍保留。已发布有值变空须有核实过的撤回说明。数值或撤回原因变化才产生修订，重复响应不改原事实出处。

## 已有来源能力

| 已有数据集 | 点位字段 | 频率 | 序列 |
|---|---|---|---|
| 中证 `index.cs.history` | `open/high/low/close`，部分日期只有close | 日 | `history.points` |
| 申万 `index.sw.analysis` | `closeindex` | 日 | `analysis.points` |
| 乐咕 `index.legu.pe` | `close` | 月 | `pe.points` |
| 乐咕 `index.legu.pb` | `close` | 月 | `pb.points` |
| 先前新浪探测 `index.price.range_probe` | 固定KLC解码得到OHLC | 日 | `history.points` |

申万一、二级都有收盘点位；历史缺日、已停止指数及目录历史有效期／父级待核事项仍存在，不能用任务完成标记全窗口覆盖。策略共用同一结构：上证红利有中证日线，深证红利有乐咕月频，没有证据的策略日线保持缺口。中证看板／近期估值文件本期没有接入点位解析。

乐咕保留实际观察日及同月移动快照，读取时按月返回查询范围内最后实际观察点，不转成日频。点位与估值首日可以不同；发布前回溯点位与发布后行情需要区分，第一条观测不作为成立日。

## 历史回放

```powershell
python -m quarterly_dashboard.index_price_replay --start 2016-10-11 --end 2026-10-09
```

只读取成功批次、解析通过且双哈希校验通过的已有响应，不执行网络请求。范围取调用范围与原成功批次范围交集；不迁移旧采集表。申万按原批次复核分页总数、跨页重复后发布；中证排除休市行和请求边界复制占位；缺值不填补。

每个原批次建立公共回放批次；幂等键包含原批次ID、实际范围、解析及定义版本。复用来源记录以 `reused_source_id` 指向原来源，共用文件路径和双哈希，保持原参数及真实取得时间，不重复复制文件或冒充新请求。事实复合外键保证来源属于本批次。异常保留元数据和已发布月份；恢复跳过相同范围已验证完成的回放批次。成功回放只证明已有归档入库，覆盖状态保持unknown／empty。

## 后续采集与读取

现有中证历史、申万分析及乐咕PE/PB采集，在同一个估值响应上同步解析点位，不增加行情请求。申万全部页验证通过后，估值和点位按月在同一事务发布；点位失败回滚当月的两类事实。沿用原采集的限速、取消、恢复和拒绝冷却。打开页面／启动后台不自动回放，历史回放需明确启动。

未来增量仍按现有估值采集水位推进，写入当次点位；旧归档缺写由回放补充，不为点位全量重抓。不含点位的估值来源不自动请求其他行情接口。未来独立行情适配器须接入公共调度，按点位序列自身水位补尾部，核定字段和收益口径后登记定义。

`read_prices`明确选择指数、提供方、序列、频率、收益口径、定义版本及日期范围，本地读取，不拼接来源。本期没有新增点位页面、HTTP接口或缓存；原估值图不变。点位写入不更新仅服务估值缓存的 `index_read_revisions`，未来点位缓存须单独登记失效规则。

## 维护与验证

三个新表归入「指数数据」。定义表按 `updated_at`，序列表按 `created_at`，事实表按 `updated_at, obtained_at`；空表显示暂无数据，缺时间不使用交易日替代。迁移只新增表，不自动导入或改写原估值、财务和个股价格。

隔离库验证迁移、修订、空值撤回、类别兼容、分页原子性、月频快照、零网络回放、幂等和维护统计。正式迁移前由已有机制备份SQLite；资料迁移须同时复制数据库旁 `collection_sources/` 并校验哈希。验收资料在 `data/verification/reports/`，临时测试副本完成后清理，正式备份和来源保留。

只读验收命令：

```powershell
python -m quarterly_dashboard.index_price_audit --report data/verification/reports/index-points-audit.json
```

核对原始文件双哈希、逐点位值及真实取得时间；相同重叠来源允许复用已保留的相同业务版本。审计同时输出按类别／提供方／频率的范围与逐字段有效数、外键和完整性、维护页新表统计。零网络请求。2026-10-11隔离真实归档演练共回放298个原批次、352335条点位，二次回放全部跳过；原估值内容哈希未变。详细验收在 `data/verification/reports/2026-10-11-index-points-rehearsal-*.json`。

2026-10-11日常库已备份迁移并离线导入同一批352335条最新事实：宽基47820条／40序列，行业301603条／162序列，策略2912条／5序列。数量包含不同提供方、接口及日／月频序列，不代表独立交易日或全窗口覆盖。正式逐来源值／时间／双哈希核对、外键与完整性通过，原估值全内容SHA256保持一致；8765后台已重启到v27，主页／指数页／目录接口正常。正式报告为 `2026-10-11-index-points-daily-replay.json`、`2026-10-11-index-points-daily-audit.json`、`2026-10-11-index-points-daily-summary.json`，均在 `data/verification/reports/`；日常日志为 `data/verification/logs/daily-8765-index-points.out.log` 和 `.err.log`。
